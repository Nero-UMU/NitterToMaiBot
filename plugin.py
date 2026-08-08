"""定期把 Nitter 订阅账号的新推文转发到指定 QQ 群。"""

from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Set, Tuple
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

import asyncio
import base64
import mimetypes
import re

from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import HookMode
from pydantic import field_validator

from .config_mirror import SubscriptionConfigMirror
from .media_cache import CachedMedia, MediaCacheLimitError, MediaCacheService, seconds_until_cleanup
from .models import MediaAttachment, NitterPost, ScanSummary
from .nitter_client import MediaTooLargeError, NitterClient
from .state_store import StateStore
from .subscription_store import SubscriptionStore


QQ_ID_PATTERN = re.compile(r"^\d+$")
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,15}$")
STATUS_URL_PATTERN = re.compile(
    r"https?://[^\s<>/]+/(?P<account>[A-Za-z0-9_]{1,15})/status/(?P<post_id>\d+)",
    re.IGNORECASE,
)
ACCOUNT_SEPARATOR_PATTERN = re.compile(r"[\s,，]+")
MESSAGE_TOKEN = "message"
FORWARD_TOKEN = "forward"
BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")
MAX_INLINE_MEDIA_BYTES = 10 * 1024 * 1024
OFFICIAL_STATUS_BASE_URL = "https://x.com"
FOLLOW_LIST_FORWARD_THRESHOLD = 20
TRANSLATION_SYSTEM_PROMPT = (
    "你是严格的推文翻译器。请把用户提供的推文正文翻译成自然、准确的简体中文。"
    "保留人名、账号、话题标签、URL、换行和原文语气，不要解释、概括、审查或回答推文内容。"
    "只输出翻译结果，不要添加标题、引号或‘翻译’前缀。"
)
TranslationModelTask = Literal["utils", "replyer", "planner"]


class PluginSectionConfig(PluginConfigBase):
    """插件开关配置。"""

    __ui_label__ = "插件设置"
    __ui_icon__ = "rss"
    __ui_order__ = 0

    enabled: bool = Field(
        default=False,
        description="控制定时扫描、自动转发和群消息自动解析功能是否运行；手动命令仍由命令自身校验。",
        json_schema_extra={"label": "启用插件", "hint": "开启后插件会按照下方轮询设置检查订阅账号。"},
    )
    config_version: str = Field(
        default="1.6.0",
        description="用于插件自动升级配置结构，由程序维护。",
        json_schema_extra={"disabled": True, "label": "配置版本", "hint": "只读字段，请勿手动修改。"},
    )


class NitterSectionConfig(PluginConfigBase):
    """Nitter 订阅与轮询配置。"""

    __ui_label__ = "Nitter 订阅"
    __ui_icon__ = "radio"
    __ui_order__ = 1

    base_url: str = Field(
        default="https://nitter.net",
        description="插件读取时间线、推文详情和媒体文件时使用的 Nitter 实例根地址。",
        json_schema_extra={
            "hint": "必须包含 http:// 或 https://，末尾斜杠会自动移除。",
            "label": "Nitter 实例地址",
            "placeholder": "https://nitter.net",
        },
    )
    accounts: List[str] = Field(
        default_factory=list,
        description="仅用于从 1.2.x 及更早版本迁移全局账号订阅",
        json_schema_extra={"hidden": True},
    )
    poll_interval_seconds: int = Field(
        default=600,
        ge=30,
        le=86400,
        description="两次自动扫描之间的等待时间，单位为秒。",
        json_schema_extra={"label": "轮询间隔（秒）", "hint": "默认 600 秒；过短可能增加 Nitter 负载。"},
    )
    request_timeout_seconds: int = Field(
        default=20,
        ge=3,
        le=120,
        description="单次访问 Nitter 或下载媒体时允许等待的最长时间。",
        json_schema_extra={"label": "请求超时（秒）", "hint": "网络较慢或视频较多时可适当增大。"},
    )
    request_attempts: int = Field(
        default=3,
        ge=1,
        le=5,
        description="Nitter 请求遇到临时网络错误时的最大尝试次数。",
        json_schema_extra={"label": "请求尝试次数", "hint": "包含第一次请求，允许范围为 1～5 次。"},
    )
    max_posts_per_scan: int = Field(
        default=10,
        ge=1,
        le=50,
        description="单个账号在一次自动扫描或手动获取中最多处理的推文数量。",
        json_schema_extra={"label": "单账号单轮上限", "hint": "用于限制积压消息数量，也限制 /推特推文 的数量参数。"},
    )
    max_seen_posts_per_account: int = Field(
        default=500,
        ge=50,
        le=5000,
        description="每个账号在去重状态中保留的已处理推文 ID 数量。",
        json_schema_extra={"label": "已处理记录上限", "hint": "记录越多越不容易重复转发，但状态文件会相应增大。"},
    )
    scan_on_load: bool = Field(
        default=True,
        description="插件加载或重新加载完成后是否立即执行一次订阅扫描。",
        json_schema_extra={"label": "加载后立即扫描", "hint": "关闭后会等待一个完整轮询间隔再进行首次扫描。"},
    )
    send_existing_on_first_run: bool = Field(
        default=False,
        description="账号第一次建立去重记录时，是否把 Nitter 时间线中已经存在的推文也发送出去。",
        json_schema_extra={
            "label": "首次订阅发送历史推文",
            "hint": "建议关闭；关闭时只建立基线，之后出现的新推文才会发送。",
        },
    )
    include_retweets: bool = Field(
        default=True,
        description="控制自动订阅扫描和 /推特推文 是否发送账号转发的其他账号推文。",
        json_schema_extra={
            "label": "转发转推",
            "hint": "开启时发送账号自己发布的内容和转推；关闭时忽略转推。/推特解析不受此选项影响。",
        },
    )

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        """确保实例地址是完整的 HTTP(S) URL。"""

        normalized_value = value.strip().rstrip("/")
        parsed_url = urlsplit(normalized_value)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError("Nitter 地址必须是完整的 http:// 或 https:// URL")
        return normalized_value

    @field_validator("accounts")
    @classmethod
    def validate_accounts(cls, values: List[str]) -> List[str]:
        """规范化账号 ID 并拒绝非法路径字符。"""

        normalized_accounts: List[str] = []
        for value in values:
            account = value.strip().lstrip("@")
            if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", account):
                raise ValueError(f"无效的推特账号 ID: {value}")
            if account.lower() not in {item.lower() for item in normalized_accounts}:
                normalized_accounts.append(account)
        return normalized_accounts


class TranslationSectionConfig(PluginConfigBase):
    """推文正文翻译配置。"""

    __ui_label__ = "推文翻译"
    __ui_icon__ = "languages"
    __ui_order__ = 2

    enabled: bool = Field(
        default=False,
        description="是否使用 MaiBot 已配置的模型把推文正文翻译成简体中文。",
        json_schema_extra={
            "label": "启用推文翻译",
            "hint": "默认关闭；开启后会在推文原文下方附加中文翻译。",
        },
    )
    model: TranslationModelTask = Field(
        default="utils",
        description="执行推文翻译时使用的 MaiBot 模型任务配置。",
        json_schema_extra={
            "label": "翻译模型",
            "hint": "用于决定由哪个模型任务执行翻译。",
        },
    )
    prompt: str = Field(
        default=TRANSLATION_SYSTEM_PROMPT,
        min_length=1,
        description="发送给翻译模型的系统提示词，可根据需要自行调整翻译要求和输出格式。",
        json_schema_extra={
            "label": "翻译提示词",
            "hint": "默认已填写插件内置提示词；修改后会用于之后的所有推文翻译。",
            "x-widget": "textarea",
        },
    )


class DeliverySectionConfig(PluginConfigBase):
    """QQ 群与附件投递配置。"""

    __ui_label__ = "转发目标"
    __ui_icon__ = "send"
    __ui_order__ = 3

    qq_groups: List[str] = Field(
        default_factory=list,
        description="仅用于从 1.2.x 及更早版本迁移全局目标群",
        json_schema_extra={"hidden": True},
    )
    qq_account_id: str = Field(
        default="",
        description="自动推送时使用的机器人 QQ 号；仅在 MaiBot 同时接入多个 QQ 账号时需要指定。",
        json_schema_extra={
            "label": "发送机器人 QQ 号",
            "hint": "只有一个机器人账号时请留空，插件会自动选择；这里不是目标群号。",
            "placeholder": "例如：123456",
        },
    )
    send_images: bool = Field(
        default=True,
        description="是否下载推文图片并作为 QQ 图片消息发送。",
        json_schema_extra={"label": "发送图片", "hint": "关闭后推文正文仍会正常发送。"},
    )
    send_videos: bool = Field(
        default=True,
        description="是否解析推文视频地址，并以 QQ 文件消息发送。",
        json_schema_extra={"label": "发送视频", "hint": "视频会先下载到插件缓存，再通过临时文件地址发送。"},
    )
    send_other_files: bool = Field(
        default=True,
        description="是否发送 RSS 或状态页中识别出的非图片、非视频附件。",
        json_schema_extra={"label": "发送其他附件", "hint": "关闭后只保留正文以及已启用的图片或视频。"},
    )
    forward_batch_threshold: int = Field(
        default=1,
        ge=1,
        le=50,
        description="同一轮向同一个群发送的推文数量超过该值时，改用一条 QQ 合并转发消息。",
        json_schema_extra={"label": "合并转发阈值", "hint": "默认 1，表示 1 条普通发送，2 条及以上打包。"},
    )
    max_media_size_mb: int = Field(
        default=100,
        ge=1,
        le=1024,
        description="单个图片、视频或其他附件允许下载到插件缓存的最大大小。",
        json_schema_extra={
            "label": "媒体大小上限（MiB）",
            "hint": "默认 100 MiB，最大 1024 MiB；图片超过 10 MiB 时仍会作为文件发送。",
        },
    )

    @field_validator("qq_groups")
    @classmethod
    def validate_qq_groups(cls, values: List[str]) -> List[str]:
        """校验并去重 QQ 群号。"""

        normalized_groups: List[str] = []
        for value in values:
            group_id = str(value).strip()
            if not QQ_ID_PATTERN.fullmatch(group_id):
                raise ValueError(f"无效的 QQ 群号: {value}")
            if group_id not in normalized_groups:
                normalized_groups.append(group_id)
        return normalized_groups

    @field_validator("qq_account_id")
    @classmethod
    def validate_qq_account_id(cls, value: str) -> str:
        """校验可选的机器人 QQ 号。"""

        normalized_value = value.strip()
        if normalized_value and not QQ_ID_PATTERN.fullmatch(normalized_value):
            raise ValueError("机器人 QQ 号只能包含数字")
        return normalized_value


class MediaCacheSectionConfig(PluginConfigBase):
    """媒体落盘、临时访问地址与自动清理配置。"""

    __ui_label__ = "媒体缓存"
    __ui_icon__ = "hard-drive-download"
    __ui_order__ = 4

    bind_host: str = Field(
        default="127.0.0.1",
        description="插件临时文件服务在服务器上监听的网络地址。",
        json_schema_extra={
            "label": "媒体服务监听地址",
            "hint": "QQ 适配器与 MaiBot 同机时保持 127.0.0.1；跨主机使用时需要按网络环境配置。",
            "placeholder": "127.0.0.1",
        },
    )
    port: int = Field(
        default=18080,
        ge=1024,
        le=65535,
        description="插件临时文件 HTTP 服务使用的端口。",
        json_schema_extra={
            "label": "媒体服务端口",
            "hint": "默认 18080；必须确保没有被其他程序占用。",
        },
    )
    public_base_url: str = Field(
        default="http://127.0.0.1:18080",
        description="发送给 QQ 适配器、用于下载缓存媒体的 HTTP 根地址。",
        json_schema_extra={
            "label": "媒体外部访问地址",
            "hint": "QQ 适配器与 MaiBot 同机时保持默认值；该地址不能包含路径、查询参数或片段。",
            "placeholder": "http://127.0.0.1:18080",
        },
    )
    cleanup_enabled: bool = Field(
        default=True,
        description="是否每天定时删除已经下载到插件数据目录的媒体缓存。",
        json_schema_extra={
            "label": "定期清理下载文件",
            "hint": "默认开启；关闭后媒体文件会持续保留并占用磁盘空间。",
        },
    )
    cleanup_time: str = Field(
        default="02:00",
        description="每天执行媒体缓存清理的北京时间，格式为 HH:MM。",
        json_schema_extra={
            "label": "每日清理时间",
            "hint": "默认每天北京时间凌晨 2 点清理全部已下载媒体。",
            "placeholder": "02:00",
        },
    )

    @field_validator("bind_host")
    @classmethod
    def validate_bind_host(cls, value: str) -> str:
        """拒绝空监听地址和包含空白的主机名。"""

        normalized_value = value.strip()
        if not normalized_value or re.search(r"\s", normalized_value):
            raise ValueError("媒体服务监听地址不能为空或包含空白")
        return normalized_value

    @field_validator("public_base_url")
    @classmethod
    def validate_public_base_url(cls, value: str) -> str:
        """要求媒体外部地址是没有附加路径的完整 HTTP(S) 根地址。"""

        normalized_value = value.strip().rstrip("/")
        parsed_url = urlsplit(normalized_value)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError("媒体外部访问地址必须是完整的 http:// 或 https:// URL")
        if parsed_url.path or parsed_url.query or parsed_url.fragment:
            raise ValueError("媒体外部访问地址不能包含路径、查询参数或片段")
        return normalized_value

    @field_validator("cleanup_time")
    @classmethod
    def validate_cleanup_time(cls, value: str) -> str:
        """校验并规范化每日清理时间。"""

        normalized_value = value.strip()
        match = re.fullmatch(r"(?P<hour>\d{1,2}):(?P<minute>\d{2})", normalized_value)
        if match is None:
            raise ValueError("每日清理时间必须使用 HH:MM 格式")
        hour = int(match.group("hour"))
        minute = int(match.group("minute"))
        if hour > 23 or minute > 59:
            raise ValueError("每日清理时间必须是有效的 24 小时时间")
        return f"{hour:02d}:{minute:02d}"


class InteractionSectionConfig(PluginConfigBase):
    """群内订阅命令与推文链接解析配置。"""

    __ui_label__ = "群内交互"
    __ui_icon__ = "message-circle"
    __ui_order__ = 5

    allow_group_commands: bool = Field(
        default=True,
        description="是否允许在 QQ 群内使用关注、取关、订阅列表和推送开关命令。",
        json_schema_extra={"label": "允许群内管理订阅", "hint": "关闭后群成员不能修改订阅，但已有自动推送不受影响。"},
    )
    command_only_local_operator: bool = Field(
        default=False,
        description="是否只允许被 MaiBot 识别为本地操作员的用户执行会修改订阅的群命令。",
        json_schema_extra={"label": "仅限本地操作员管理", "hint": "订阅列表查询不受该限制。"},
    )
    max_accounts_per_group: int = Field(
        default=0,
        ge=0,
        le=500,
        description="单个 QQ 群允许同时订阅的推特账号数量上限。",
        json_schema_extra={"label": "每群订阅账号上限", "hint": "设置为 0 表示不限制；其他数值用于限制单群订阅数量。"},
    )
    auto_parse_tweet_links: bool = Field(
        default=True,
        description="是否自动识别 QQ 私聊或群聊普通消息中的第一条 Twitter、X 或 Nitter 推文链接并发送内容。",
        json_schema_extra={
            "label": "自动解析推文链接",
            "hint": "默认开启；识别到链接后由插件处理并阻止消息继续交给 LLM。",
        },
    )


class SubscriptionGroupViewConfig(PluginConfigBase):
    """后台只读展示的一条 QQ 群记录。"""

    group_id: str = Field(default="", description="订阅推特消息的 QQ 群号")
    enabled: bool = Field(default=True, description="该群是否启用推送")


class SubscriptionAccountViewConfig(PluginConfigBase):
    """后台只读展示的一条推特账号记录。"""

    account: str = Field(default="", description="推特账号 @ID")
    display_name: str = Field(default="", description="推特账号当前显示名")
    qq_groups: List[str] = Field(default_factory=list, description="订阅该账号的 QQ 群号")


class SubscriptionViewSectionConfig(PluginConfigBase):
    """由 subscriptions.json 自动生成的后台只读镜像。"""

    __ui_label__ = "订阅列表（只读）"
    __ui_icon__ = "list-tree"
    __ui_order__ = 6

    groups: List[SubscriptionGroupViewConfig] = Field(
        default_factory=list,
        description="已订阅推特消息的 QQ 群；请使用群内命令修改",
        json_schema_extra={
            "disabled": True,
            "hint": "只读展示；enabled 表示该群当前是否允许自动推送。",
            "label": "订阅群列表",
        },
    )
    accounts: List[SubscriptionAccountViewConfig] = Field(
        default_factory=list,
        description="推特账号及订阅该账号的 QQ 群；请使用群内命令修改",
        json_schema_extra={
            "disabled": True,
            "hint": "只读展示；每个账号后的 QQ 群列表表示哪些群订阅了该账号。",
            "label": "账号订阅列表",
        },
    )


class NitterToMaiBotConfig(PluginConfigBase):
    """插件完整配置模型。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    nitter: NitterSectionConfig = Field(default_factory=NitterSectionConfig)
    translation: TranslationSectionConfig = Field(default_factory=TranslationSectionConfig)
    delivery: DeliverySectionConfig = Field(default_factory=DeliverySectionConfig)
    media_cache: MediaCacheSectionConfig = Field(default_factory=MediaCacheSectionConfig)
    interaction: InteractionSectionConfig = Field(default_factory=InteractionSectionConfig)
    subscriptions: SubscriptionViewSectionConfig = Field(default_factory=SubscriptionViewSectionConfig)


class NitterToMaiBotPlugin(MaiBotPlugin):
    """Nitter 推文到 MaiBot QQ 群的转发插件。"""

    config_model = NitterToMaiBotConfig

    def __init__(self) -> None:
        super().__init__()
        self._poll_task: Optional[asyncio.Task[None]] = None
        self._wake_event = asyncio.Event()
        self._scan_lock = asyncio.Lock()
        self._subscription_lock = asyncio.Lock()
        self._state_store: Optional[StateStore] = None
        self._subscription_store: Optional[SubscriptionStore] = None
        self._media_cache_service: Optional[MediaCacheService] = None
        self._media_cleanup_task: Optional[asyncio.Task[None]] = None
        self._config_mirror = SubscriptionConfigMirror(Path(__file__).resolve().with_name("config.toml"))

    @property
    def config(self) -> NitterToMaiBotConfig:
        """返回已经由 SDK 校验的强类型插件配置。"""

        config = super().config
        if not isinstance(config, NitterToMaiBotConfig):
            raise TypeError("NitterToMaiBot 配置实例类型不正确")
        return config

    async def on_load(self) -> None:
        """加载持久化状态并启动轮询任务。"""

        self.ctx.paths.data_dir.mkdir(parents=True, exist_ok=True)
        await self._start_media_services()
        self._state_store = StateStore(
            path=self.ctx.paths.data_dir / "state.json",
            max_seen_per_account=self.config.nitter.max_seen_posts_per_account,
        )
        await asyncio.to_thread(self._state_store.load)
        self._subscription_store = SubscriptionStore(
            path=self.ctx.paths.data_dir / "subscriptions.json",
        )
        await asyncio.to_thread(self._subscription_store.load)
        migrated_legacy = self._subscription_store.merge_legacy_global_subscriptions(
            self.config.nitter.accounts,
            self.config.delivery.qq_groups,
        )
        if self._subscription_store.needs_save or migrated_legacy:
            await asyncio.to_thread(self._subscription_store.save)
        await self._sync_subscription_mirror()

        if self.config.plugin.enabled:
            self._start_polling()
        self.ctx.logger.info(
            "NitterToMaiBot 已加载：enabled=%s，subscription_accounts=%d，subscription_groups=%d，legacy_migrated=%s",
            self.config.plugin.enabled,
            self._subscription_store.account_count(),
            self._subscription_store.group_count(),
            migrated_legacy,
        )

    async def on_unload(self) -> None:
        """停止后台轮询任务。"""

        await self._stop_polling()
        await self._stop_media_services()
        self.ctx.logger.info("NitterToMaiBot 已卸载")

    async def on_config_update(self, scope: str, config_data: Dict[str, object], version: str) -> None:
        """配置热更新后立即应用开关和轮询参数。"""

        del config_data
        if scope != CONFIG_RELOAD_SCOPE_SELF:
            return

        if self._subscription_store is not None:
            async with self._subscription_lock:
                await self._sync_subscription_mirror()
        if self._state_store is not None:
            self._state_store.max_seen_per_account = self.config.nitter.max_seen_posts_per_account
        if self.config.plugin.enabled:
            async with self._scan_lock:
                await self._restart_media_services()
            self._start_polling()
            self._wake_event.set()
        else:
            await self._stop_polling()
            await self._stop_media_services()
        self.ctx.logger.info("NitterToMaiBot 配置已更新：version=%s", version)

    @Command(
        "nitter_to_maibot_status",
        description="查看 Nitter 推文转发插件的运行状态",
        pattern=r"^/nitter_status$",
    )
    async def handle_status(self, stream_id: str = "", **kwargs: Any) -> Tuple[bool, str, int]:
        """向当前聊天返回插件状态。"""

        del kwargs
        subscription_store = self._require_subscription_store()
        translation_status = (
            f"开启（{self.config.translation.model}）"
            if self.config.translation.enabled
            else "关闭"
        )
        cleanup_status = (
            f"每天 {self.config.media_cache.cleanup_time}（北京时间）"
            if self.config.media_cache.cleanup_enabled
            else "关闭"
        )
        status_text = (
            "NitterToMaiBot 状态\n"
            f"启用：{'是' if self.config.plugin.enabled else '否'}\n"
            f"Nitter：{self.config.nitter.base_url}\n"
            f"订阅账号：{subscription_store.account_count()} 个\n"
            f"订阅 QQ 群：{subscription_store.group_count()} 个\n"
            f"推文翻译：{translation_status}\n"
            f"媒体下载上限：{self.config.delivery.max_media_size_mb} MiB\n"
            f"媒体缓存清理：{cleanup_status}\n"
            f"合并转发阈值：超过 {self.config.delivery.forward_batch_threshold} 条\n"
            f"轮询间隔：{self.config.nitter.poll_interval_seconds} 秒"
        )
        if stream_id:
            await self.ctx.send.text(status_text, stream_id)
        return True, status_text, 2

    @Command(
        "nitter_to_maibot_scan",
        description="立即扫描一次 Nitter 订阅并转发新推文",
        pattern=r"^/nitter_scan$",
    )
    async def handle_scan(self, stream_id: str = "", **kwargs: Any) -> Tuple[bool, str, int]:
        """手动触发一次扫描，便于联调。"""

        del kwargs
        if not self.config.plugin.enabled:
            response = "NitterToMaiBot 当前未启用，请先修改插件配置。"
            if stream_id:
                await self.ctx.send.text(response, stream_id)
            return False, response, 2

        summary = await self._scan_once()
        response = (
            "Nitter 扫描完成："
            f"账号 {summary.scanned_accounts} 个，"
            f"读取推文 {summary.fetched_posts} 条，"
            f"完成转发 {summary.forwarded_posts} 条，"
            f"失败账号 {summary.failed_accounts} 个。"
        )
        if stream_id:
            await self.ctx.send.text(response, stream_id)
        return summary.failed_accounts == 0, response, 2

    @Command(
        "nitter_to_maibot_follow",
        description="为当前 QQ 群订阅一个或多个推特账号",
        pattern=r"^/(?:推特关注|twitter_follow)(?:\s+(?P<accounts>.+))?$",
        timeout_ms=120000,
    )
    async def handle_follow(
        self,
        stream_id: str = "",
        group_id: str = "",
        platform: str = "",
        is_local_operator: bool = False,
        matched_groups: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Tuple[bool, str, int]:
        """校验账号时间线后，为当前群建立持久化订阅。"""

        del kwargs
        context_error = self._group_manage_error(group_id, platform, is_local_operator)
        if context_error:
            return await self._command_response(False, context_error, stream_id)

        raw_accounts = "" if matched_groups is None else str(matched_groups.get("accounts") or "")
        try:
            accounts = self._parse_accounts(raw_accounts)
        except ValueError as exc:
            return await self._command_response(False, str(exc), stream_id)
        if not accounts:
            return await self._command_response(
                False,
                "请提供推特账号 ID。用法：/推特关注 <账号1> [账号2 ...]",
                stream_id,
            )
        if len(accounts) > 10:
            return await self._command_response(False, "单次最多添加 10 个账号。", stream_id)

        subscription_store = self._require_subscription_store()
        current_accounts = subscription_store.accounts_for_group(group_id)
        current_keys = {account.lower() for account in current_accounts}
        new_accounts = [account for account in accounts if account.lower() not in current_keys]
        account_limit = self.config.interaction.max_accounts_per_group
        if account_limit > 0 and len(current_accounts) + len(new_accounts) > account_limit:
            return await self._command_response(
                False,
                f"当前群最多可订阅 {account_limit} 个账号。",
                stream_id,
            )

        client = self._create_client()
        fetched_timelines: Dict[str, List[NitterPost]] = {}
        fetched_profile_names: Dict[str, str] = {}
        result_lines: List[str] = []
        for account in accounts:
            if account.lower() in current_keys:
                result_lines.append(f"○ @{account} 已在当前群的订阅列表中")
                continue
            try:
                posts = await client.fetch_timeline(account)
            except Exception:
                self.ctx.logger.warning("群内订阅时读取 @%s 的 Nitter RSS 失败", account, exc_info=True)
                result_lines.append(f"× @{account} 获取失败，请稍后重试")
                continue
            if not posts:
                result_lines.append(f"× @{account} 没有可读取的公开推文")
                continue
            fetched_timelines[account] = posts
            profile_name = client.profile_name(account)
            if profile_name:
                fetched_profile_names[account] = profile_name

        added_count = 0
        if fetched_timelines:
            async with self._scan_lock:
                async with self._subscription_lock:
                    state_store = self._require_state_store()
                    for account, posts in fetched_timelines.items():
                        if subscription_store.subscribe(group_id, account):
                            added_count += 1
                            result_lines.append(f"✓ 已为当前群订阅 @{account}")
                        profile_name = fetched_profile_names.get(account, "")
                        if profile_name:
                            subscription_store.set_display_name(account, profile_name)
                        if not state_store.has_account(account):
                            baseline = []
                            if not self.config.nitter.send_existing_on_first_run:
                                baseline = list(reversed([post.post_id for post in posts]))
                            state_store.mark_baseline(account, baseline)
                    await asyncio.to_thread(subscription_store.save)
                    await asyncio.to_thread(state_store.save)
                    await self._sync_subscription_mirror()

        if added_count > 0:
            self._wake_event.set()
        response = f"订阅处理完成：新增 {added_count} 个。\n" + "\n".join(result_lines)
        return await self._command_response(added_count > 0, response, stream_id)

    @Command(
        "nitter_to_maibot_unfollow",
        description="从当前 QQ 群移除一个或多个推特账号订阅",
        pattern=r"^/(?:推特取关|twitter_unfollow)(?:\s+(?P<accounts>.+))?$",
    )
    async def handle_unfollow(
        self,
        stream_id: str = "",
        group_id: str = "",
        platform: str = "",
        is_local_operator: bool = False,
        matched_groups: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Tuple[bool, str, int]:
        """从当前群的订阅中移除账号。"""

        del kwargs
        context_error = self._group_manage_error(group_id, platform, is_local_operator)
        if context_error:
            return await self._command_response(False, context_error, stream_id)
        raw_accounts = "" if matched_groups is None else str(matched_groups.get("accounts") or "")
        try:
            accounts = self._parse_accounts(raw_accounts)
        except ValueError as exc:
            return await self._command_response(False, str(exc), stream_id)
        if not accounts:
            return await self._command_response(
                False,
                "请提供推特账号 ID。用法：/推特取关 <账号1> [账号2 ...]",
                stream_id,
            )

        subscription_store = self._require_subscription_store()
        removed_accounts: List[str] = []
        missing_accounts: List[str] = []
        async with self._subscription_lock:
            for account in accounts:
                if subscription_store.unsubscribe(group_id, account):
                    removed_accounts.append(account)
                else:
                    missing_accounts.append(account)
            if removed_accounts:
                await asyncio.to_thread(subscription_store.save)
                await self._sync_subscription_mirror()

        lines = [f"已从当前群取关 @{account}" for account in removed_accounts]
        lines.extend(f"当前群未订阅 @{account}" for account in missing_accounts)
        response = "\n".join(lines)
        return await self._command_response(bool(removed_accounts), response, stream_id)

    @Command(
        "nitter_to_maibot_list_follows",
        description="查看当前 QQ 群的动态推特订阅",
        pattern=r"^/(?:推特订阅|twitter_follows)$",
        timeout_ms=120000,
    )
    async def handle_list_follows(
        self,
        stream_id: str = "",
        group_id: str = "",
        platform: str = "",
        **kwargs: Any,
    ) -> Tuple[bool, str, int]:
        """列出当前群订阅及推送状态。"""

        del kwargs
        if platform.lower() != "qq" or not group_id:
            return await self._command_response(False, "该命令只能在 QQ 群聊中使用。", stream_id)
        subscription_store = self._require_subscription_store()
        accounts = subscription_store.accounts_for_group(group_id)
        if not accounts:
            response = "当前群还没有订阅推特账号。"
        else:
            status = "开启" if subscription_store.is_push_enabled(group_id) else "关闭"
            header_lines = [f"当前群推送：{status}", f"订阅账号（{len(accounts)} 个）："]
            account_lines = [
                f"{index}.@{account} {subscription_store.display_name(account) or '名称未获取'}"
                for index, account in enumerate(accounts, start=1)
            ]
            response = "\n".join([*header_lines, *account_lines])
            if len(accounts) > FOLLOW_LIST_FORWARD_THRESHOLD:
                sent = await self._send_follow_list_forward(
                    header_lines,
                    account_lines,
                    stream_id,
                )
                if not sent:
                    return await self._command_response(
                        False,
                        "发送订阅列表合并转发失败。",
                        stream_id,
                    )
                return True, response, 2
        return await self._command_response(True, response, stream_id)

    @Command(
        "nitter_to_maibot_toggle_push",
        description="开启或关闭当前 QQ 群的订阅推送",
        pattern=r"^/(?:推特推送|twitter_push)(?:\s+(?P<status>开启|关闭|on|off))?$",
    )
    async def handle_toggle_push(
        self,
        stream_id: str = "",
        group_id: str = "",
        platform: str = "",
        is_local_operator: bool = False,
        matched_groups: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Tuple[bool, str, int]:
        """切换当前群的动态推送开关。"""

        del kwargs
        context_error = self._group_manage_error(group_id, platform, is_local_operator)
        if context_error:
            return await self._command_response(False, context_error, stream_id)
        raw_status = "" if matched_groups is None else str(matched_groups.get("status") or "").lower()
        if raw_status not in {"开启", "关闭", "on", "off"}:
            return await self._command_response(
                False,
                "请指定开启或关闭。用法：/推特推送 开启|关闭",
                stream_id,
            )
        enabled = raw_status in {"开启", "on"}
        subscription_store = self._require_subscription_store()
        async with self._subscription_lock:
            if not subscription_store.set_push_enabled(group_id, enabled):
                return await self._command_response(
                    False,
                    "当前群还没有订阅推特账号，请先使用 /推特关注 添加订阅。",
                    stream_id,
                )
            await asyncio.to_thread(subscription_store.save)
            await self._sync_subscription_mirror()
        if enabled:
            self._wake_event.set()
        response = f"当前群的动态推特推送已{'开启' if enabled else '关闭'}。"
        return await self._command_response(True, response, stream_id)

    @Command(
        "nitter_to_maibot_posts",
        description="获取并发送指定账号的最新推文",
        pattern=r"^/(?:推特推文|twitter_posts)(?:\s+(?P<account>\S+))?(?:\s+(?P<count>\S+))?$",
        timeout_ms=120000,
    )
    async def handle_posts(
        self,
        stream_id: str = "",
        matched_groups: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Tuple[bool, str, int]:
        """获取账号最新若干条推文并发送到当前聊天流，不写入轮询游标。"""

        del kwargs
        raw_account = "" if matched_groups is None else str(matched_groups.get("account") or "")
        try:
            accounts = self._parse_accounts(raw_account)
        except ValueError as exc:
            return await self._command_response(False, str(exc), stream_id)
        if len(accounts) != 1:
            return await self._command_response(
                False,
                "请提供一个推特账号 ID。用法：/推特推文 <账号> [数量]",
                stream_id,
            )
        raw_count = "" if matched_groups is None else str(matched_groups.get("count") or "").strip()
        if raw_count and not raw_count.isdigit():
            return await self._command_response(False, "推文数量必须是正整数。", stream_id)
        post_count = int(raw_count or "1")
        if post_count < 1:
            return await self._command_response(False, "推文数量必须大于 0。", stream_id)
        if post_count > self.config.nitter.max_posts_per_scan:
            return await self._command_response(
                False,
                f"单次最多获取 {self.config.nitter.max_posts_per_scan} 条推文。",
                stream_id,
            )
        if not stream_id:
            return False, "当前聊天流不可用。", 2

        account = accounts[0]
        client = self._create_client()
        try:
            posts = await client.fetch_timeline(account)
            profile_name = client.profile_name(account)
            if profile_name:
                await self._update_subscription_display_names({account: profile_name})
            selected_posts = [
                post
                for post in posts
                if self.config.nitter.include_retweets or not post.is_retweet
            ][:post_count]
            if not selected_posts:
                return await self._command_response(False, f"未找到 @{account} 的可转发推文。", stream_id)
            await self._send_posts_preview(client, selected_posts, stream_id)
        except Exception:
            self.ctx.logger.warning(
                "读取 @%s 的最新 %d 条推文失败",
                account,
                post_count,
                exc_info=True,
            )
            return await self._command_response(False, f"获取 @{account} 的最新推文失败，请稍后重试。", stream_id)
        return True, f"已发送 @{account} 的最新 {len(selected_posts)} 条推文。", 2

    @Command(
        "nitter_to_maibot_parse_status",
        description="通过 Nitter 解析一条 Twitter/X/Nitter 推文链接",
        pattern=r"^/(?:推特解析|twitter_parse)(?:\s+(?P<url>\S+))?$",
        timeout_ms=120000,
    )
    async def handle_parse_status(
        self,
        stream_id: str = "",
        matched_groups: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Tuple[bool, str, int]:
        """手动解析推文链接并把正文、图片和视频发送到当前聊天流。"""

        del kwargs
        raw_url = "" if matched_groups is None else str(matched_groups.get("url") or "")
        reference = self._find_status_reference(raw_url)
        if reference is None:
            return await self._command_response(
                False,
                "请提供有效的 Twitter、X 或 Nitter 推文链接。",
                stream_id,
            )
        if not stream_id:
            return False, "当前聊天流不可用。", 2
        try:
            await self._parse_and_send_status(reference[0], reference[1], stream_id)
        except Exception:
            self.ctx.logger.warning("手动解析推文 %s/%s 失败", reference[0], reference[1], exc_info=True)
            return await self._command_response(False, "推文解析失败，帖子可能已删除、受限或暂时不可用。", stream_id)
        return True, "推文解析完成。", 2

    @HookHandler(
        "chat.receive.after_process",
        name="nitter_to_maibot_auto_parse_status",
        description="自动解析并拦截 QQ 私聊或群聊消息中的推文链接",
        mode=HookMode.BLOCKING,
        timeout_ms=120000,
    )
    async def handle_auto_parse_status(self, message: Dict[str, Any], **kwargs: Any) -> Dict[str, str]:
        """解析 QQ 私聊和群聊消息中的第一条推文链接，并阻止消息进入 LLM。"""

        del kwargs
        if not self.config.plugin.enabled or not self.config.interaction.auto_parse_tweet_links:
            return {"action": "continue"}
        text = str(message.get("processed_plain_text") or "").strip()
        if not text or text.startswith("/"):
            return {"action": "continue"}
        reference = self._find_status_reference(text)
        if reference is None or str(message.get("platform") or "").lower() != "qq":
            return {"action": "continue"}
        stream_id = str(message.get("session_id") or "").strip()
        if not stream_id:
            return {"action": "continue"}

        try:
            await self._parse_and_send_status(reference[0], reference[1], stream_id)
        except Exception:
            self.ctx.logger.warning("自动解析推文 %s/%s 失败", reference[0], reference[1], exc_info=True)
            try:
                await self.ctx.send.text(
                    "检测到推文链接，但解析失败；帖子可能已删除、受限或暂时不可用。",
                    stream_id,
                )
            except Exception:
                self.ctx.logger.error("向当前 QQ 聊天发送推文解析失败提示失败", exc_info=True)
        return {"action": "abort"}

    async def _command_response(
        self,
        success: bool,
        response: str,
        stream_id: str,
    ) -> Tuple[bool, str, int]:
        """发送命令反馈并返回 MaiBot Command 约定的结果。"""

        if stream_id:
            await self.ctx.send.text(response, stream_id)
        return success, response, 2

    def _group_manage_error(self, group_id: str, platform: str, is_local_operator: bool) -> str:
        """校验群内订阅管理命令的使用范围和权限。"""

        if platform.lower() != "qq" or not group_id:
            return "该命令只能在 QQ 群聊中使用。"
        if not self.config.interaction.allow_group_commands:
            return "群内订阅管理已由插件配置关闭。"
        if self.config.interaction.command_only_local_operator and not is_local_operator:
            return "该命令仅允许 MaiBot 本地操作员执行。"
        return ""

    @staticmethod
    def _parse_accounts(raw_accounts: str) -> List[str]:
        """解析空格或逗号分隔的账号 ID，执行大小写不敏感去重。"""

        accounts: List[str] = []
        known_keys = set()
        for raw_account in ACCOUNT_SEPARATOR_PATTERN.split(raw_accounts.strip()):
            if not raw_account:
                continue
            account = raw_account.lstrip("@").strip()
            if not USERNAME_PATTERN.fullmatch(account):
                raise ValueError(f"无效的推特账号 ID：{raw_account}")
            account_key = account.lower()
            if account_key not in known_keys:
                accounts.append(account)
                known_keys.add(account_key)
        return accounts

    @staticmethod
    def _find_status_reference(text: str) -> Optional[Tuple[str, str]]:
        """从文本中提取第一条 Twitter/X/Nitter 状态链接。"""

        match = STATUS_URL_PATTERN.search(text)
        if match is None:
            return None
        return match.group("account"), match.group("post_id")

    def _create_client(self) -> NitterClient:
        """按照当前热更新配置创建 Nitter 客户端。"""

        return NitterClient(
            base_url=self.config.nitter.base_url,
            timeout_seconds=self.config.nitter.request_timeout_seconds,
            request_attempts=self.config.nitter.request_attempts,
        )

    def _build_scan_targets(self) -> Dict[str, List[str]]:
        """从统一订阅存储生成当前启用的扫描与投递目标。"""

        return self._require_subscription_store().target_groups_by_account()

    async def _parse_and_send_status(self, account: str, post_id: str, stream_id: str) -> None:
        """从本地 Nitter 状态页读取并发送指定推文。"""

        client = self._create_client()
        post = await client.fetch_status(account, post_id)
        await self._send_post_preview(client, post, stream_id)

    async def _send_post_preview(self, client: NitterClient, post: NitterPost, stream_id: str) -> None:
        """发送不改变轮询状态的推文预览，单个附件失败时继续后续投递。"""

        post = await self._prepare_post(client, post, tolerate_media_errors=True)

        selected_media = self._select_media(post.media)
        media_cache: Dict[str, CachedMedia] = {}
        message_text = self._format_post_text(post)
        if not await self._send_post_message(stream_id, message_text):
            raise RuntimeError("发送推文正文失败")

        for index, media in enumerate(selected_media, start=1):
            try:
                media_sent = await self._send_media_attachment(
                    client,
                    post,
                    media,
                    index,
                    stream_id,
                    media_cache,
                )
            except Exception:
                self.ctx.logger.warning(
                    "发送推文 %s 的附件失败，将继续发送剩余内容: %s",
                    post.post_id,
                    media.url,
                    exc_info=True,
                )
                continue
            if not media_sent:
                self.ctx.logger.warning(
                    "发送推文 %s 的附件失败，将继续发送剩余内容: %s",
                    post.post_id,
                    media.url,
                )

    async def _send_posts_preview(
        self,
        client: NitterClient,
        posts: List[NitterPost],
        stream_id: str,
    ) -> None:
        """按配置阈值发送多条预览；超过阈值时合并为一条 QQ 聊天记录。"""

        if len(posts) <= self.config.delivery.forward_batch_threshold:
            for post in posts:
                await self._send_post_preview(client, post, stream_id)
            return

        prepared_posts = [
            await self._prepare_post(client, post, tolerate_media_errors=True)
            for post in posts
        ]
        if not await self._send_posts_forward(
            client,
            prepared_posts,
            stream_id,
            tolerate_media_errors=True,
        ):
            raise RuntimeError("发送推文合并转发消息失败")

    async def _prepare_post(
        self,
        client: NitterClient,
        post: NitterPost,
        *,
        tolerate_media_errors: bool,
    ) -> NitterPost:
        """依次补充推文媒体和可选的中文翻译。"""

        post = await self._prepare_post_media(
            client,
            post,
            tolerate_errors=tolerate_media_errors,
        )
        return await self._prepare_post_translation(post)

    async def _prepare_post_media(
        self,
        client: NitterClient,
        post: NitterPost,
        *,
        tolerate_errors: bool,
    ) -> NitterPost:
        """在需要时从状态页补充视频地址。"""

        has_video_attachment = any(media.media_type == "video" for media in post.media)
        if not self.config.delivery.send_videos or not post.has_video or has_video_attachment:
            return post
        try:
            return await client.enrich_status_media(post)
        except Exception:
            if not tolerate_errors:
                raise
            self.ctx.logger.warning(
                "补充推文 %s 的视频地址失败，将继续发送现有正文和附件",
                post.post_id,
                exc_info=True,
            )
            return post

    async def _prepare_post_translation(self, post: NitterPost) -> NitterPost:
        """使用配置的 MaiBot 模型任务生成简体中文翻译。"""

        source_text = post.text.strip()
        if not self.config.translation.enabled or not source_text or post.translated_text:
            return post

        result = await self.ctx.llm.generate(
            prompt=[
                {"role": "system", "content": self.config.translation.prompt},
                {"role": "user", "content": source_text},
            ],
            model=self.config.translation.model,
            temperature=0.1,
            max_tokens=2048,
        )
        if not result.get("success"):
            error = str(result.get("error") or "模型未返回成功结果")
            raise RuntimeError(f"推文 {post.post_id} 翻译失败：{error}")

        translated_text = str(result.get("response") or "").strip()
        for prefix in ("中文翻译：", "翻译："):
            if translated_text.startswith(prefix):
                translated_text = translated_text.removeprefix(prefix).strip()
                break
        if not translated_text:
            raise RuntimeError(f"推文 {post.post_id} 翻译失败：模型返回了空内容")
        if translated_text == source_text:
            return post
        return replace(post, translated_text=translated_text)

    async def _send_posts_forward(
        self,
        client: NitterClient,
        posts: List[NitterPost],
        stream_id: str,
        *,
        tolerate_media_errors: bool,
    ) -> bool:
        """把每条推文构造成一个节点，并通过 SDK 发送 QQ 合并转发。"""

        nodes = await self._build_forward_nodes(
            client,
            posts,
            tolerate_media_errors=tolerate_media_errors,
        )
        return bool(
            await self.ctx.send.forward(
                nodes,
                stream_id,
                processed_plain_text=f"[合并转发 {len(nodes)} 条推文]",
                timeout_ms=120000,
            )
        )

    async def _send_follow_list_forward(
        self,
        header_lines: List[str],
        account_lines: List[str],
        stream_id: str,
    ) -> bool:
        """把超过 20 个账号的订阅列表按每节点 20 个打包为 QQ 合并转发。"""

        nodes: List[Dict[str, Any]] = []
        for start in range(0, len(account_lines), FOLLOW_LIST_FORWARD_THRESHOLD):
            chunk_lines = account_lines[start : start + FOLLOW_LIST_FORWARD_THRESHOLD]
            if start == 0:
                chunk_lines = [*header_lines, *chunk_lines]
            nodes.append(
                {
                    "user_id": "0",
                    "nickname": "推特订阅",
                    "message_id": str(len(nodes) + 1),
                    "segments": [{"type": "text", "content": "\n".join(chunk_lines)}],
                }
            )
        return bool(
            await self.ctx.send.forward(
                nodes,
                stream_id,
                processed_plain_text=f"[推特订阅列表，共 {len(account_lines)} 个账号]",
                timeout_ms=120000,
            )
        )

    async def _build_forward_nodes(
        self,
        client: NitterClient,
        posts: List[NitterPost],
        *,
        tolerate_media_errors: bool,
    ) -> List[Dict[str, Any]]:
        """构造合并转发节点，并限制单次 RPC 中内嵌图片的总大小。"""

        nodes: List[Dict[str, Any]] = []
        media_cache: Dict[str, CachedMedia] = {}
        configured_media_bytes = self.config.delivery.max_media_size_mb * 1024 * 1024
        inline_budget = min(configured_media_bytes, MAX_INLINE_MEDIA_BYTES)
        inline_bytes = 0

        for post in posts:
            segments: List[Dict[str, Any]] = [
                {"type": "text", "content": self._format_post_text(post)}
            ]
            for index, media in enumerate(self._select_media(post.media), start=1):
                file_name = self._build_media_filename(post, media, index, media.mime_type)
                try:
                    cached_media = await self._cache_media(
                        client,
                        media,
                        file_name,
                        media_cache,
                    )
                except (MediaTooLargeError, MediaCacheLimitError):
                    self.ctx.logger.warning(
                        "推文 %s 的媒体超过缓存下载上限，改用原始 URL 文件节点: %s",
                        post.post_id,
                        media.url,
                    )
                    media_url = media.url
                    content_type = self._media_content_type(media)
                except Exception:
                    if not tolerate_media_errors:
                        raise
                    self.ctx.logger.warning(
                        "缓存推文 %s 的合并转发媒体失败，改用原始 URL 文件节点: %s",
                        post.post_id,
                        media.url,
                        exc_info=True,
                    )
                    media_url = media.url
                    content_type = self._media_content_type(media)
                else:
                    media_url = cached_media.public_url
                    content_type = cached_media.content_type
                    if media.media_type == "image" and inline_bytes + cached_media.size <= inline_budget:
                        media_data = await asyncio.to_thread(cached_media.path.read_bytes)
                        inline_bytes += len(media_data)
                        segments.append(
                            {
                                "type": "image",
                                "content": base64.b64encode(media_data).decode("ascii"),
                            }
                        )
                        continue
                    if media.media_type == "image":
                        self.ctx.logger.warning(
                            "合并转发的内嵌图片累计达到 %d MiB，后续图片改用缓存文件节点",
                            inline_budget // 1024 // 1024,
                        )

                segments.append(
                    {
                        "type": "file",
                        "data": {
                            "name": self._build_media_filename(post, media, index, content_type),
                            "mime_type": content_type,
                            "url": media_url,
                        },
                    }
                )

            nickname = post.author if post.author.startswith("@") else f"@{post.author}"
            nodes.append(
                {
                    "user_id": "0",
                    "nickname": nickname,
                    "message_id": post.post_id,
                    "segments": segments,
                }
            )
        return nodes

    async def _start_media_services(self) -> None:
        """初始化媒体缓存，并按配置启动已有缓存服务和清理任务。"""

        media_config = self.config.media_cache
        service = MediaCacheService(
            data_dir=self.ctx.paths.data_dir,
            bind_host=media_config.bind_host,
            port=media_config.port,
            public_base_url=media_config.public_base_url,
            logger=self.ctx.logger,
        )
        await asyncio.to_thread(service.initialize)
        self._media_cache_service = service
        if not self.config.plugin.enabled:
            return
        if await asyncio.to_thread(service.has_cached_files):
            await service.start()
        self._start_media_cleanup()

    async def _restart_media_services(self) -> None:
        """在配置热更新后重新应用媒体服务参数。"""

        await self._stop_media_services()
        await self._start_media_services()

    async def _stop_media_services(self) -> None:
        """停止媒体清理任务和临时 HTTP 文件服务。"""

        await self._stop_media_cleanup()
        service = self._media_cache_service
        self._media_cache_service = None
        if service is not None:
            await service.stop()

    def _start_media_cleanup(self) -> None:
        """在开启自动清理时启动每日北京时间调度任务。"""

        if not self.config.media_cache.cleanup_enabled:
            return
        if self._media_cleanup_task is not None and not self._media_cleanup_task.done():
            return
        self._media_cleanup_task = asyncio.create_task(
            self._media_cleanup_loop(),
            name="nitter_to_maibot_media_cleanup",
        )

    async def _stop_media_cleanup(self) -> None:
        """取消并等待媒体缓存清理任务结束。"""

        if self._media_cleanup_task is None:
            return
        task = self._media_cleanup_task
        self._media_cleanup_task = None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _media_cleanup_loop(self) -> None:
        """每天在配置的北京时间清理全部已完成媒体缓存。"""

        while True:
            delay_seconds = seconds_until_cleanup(self.config.media_cache.cleanup_time)
            await asyncio.sleep(delay_seconds)
            try:
                removed_files, removed_bytes = await self._require_media_cache_service().clear()
                self.ctx.logger.info(
                    "推文媒体缓存定时清理完成：files=%d，bytes=%d",
                    removed_files,
                    removed_bytes,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                self.ctx.logger.error("推文媒体缓存定时清理失败", exc_info=True)

    def _start_polling(self) -> None:
        """确保后台轮询任务只启动一次。"""

        if self._poll_task is not None and not self._poll_task.done():
            return
        self._poll_task = asyncio.create_task(self._poll_loop(), name="nitter_to_maibot_poll")

    async def _stop_polling(self) -> None:
        """取消并等待后台任务结束。"""

        if self._poll_task is None:
            return
        task = self._poll_task
        self._poll_task = None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _poll_loop(self) -> None:
        """按配置的间隔持续扫描。"""

        if not self.config.nitter.scan_on_load:
            await self._wait_for_next_scan()

        while True:
            try:
                await self._scan_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.ctx.logger.error("Nitter 后台扫描发生未处理异常", exc_info=True)
            await self._wait_for_next_scan()

    async def _wait_for_next_scan(self) -> None:
        """等待轮询超时或配置更新唤醒。"""

        try:
            await asyncio.wait_for(
                self._wake_event.wait(),
                timeout=self.config.nitter.poll_interval_seconds,
            )
        except TimeoutError:
            pass
        finally:
            self._wake_event.clear()

    async def _scan_once(self) -> ScanSummary:
        """扫描全部订阅账号，并按目标群聚合同一轮的新推文。"""

        async with self._scan_lock:
            summary = ScanSummary()
            scan_targets = self._build_scan_targets()
            if not scan_targets:
                self.ctx.logger.warning("没有已启用的群订阅，本轮扫描跳过")
                return summary

            client = self._create_client()
            stream_cache: Dict[str, str] = {}
            state_store = self._require_state_store()
            pending_deliveries: Dict[Tuple[str, str], Tuple[NitterPost, List[str]]] = {}
            fetched_profile_names: Dict[str, str] = {}

            for account, qq_groups in scan_targets.items():
                summary.scanned_accounts += 1
                try:
                    posts = await client.fetch_timeline(account)
                except Exception:
                    summary.failed_accounts += 1
                    self.ctx.logger.error("读取 @%s 的 Nitter RSS 失败", account, exc_info=True)
                    continue

                profile_name = client.profile_name(account)
                if profile_name:
                    fetched_profile_names[account] = profile_name
                summary.fetched_posts += len(posts)
                if not state_store.has_account(account):
                    if not self.config.nitter.send_existing_on_first_run:
                        state_store.mark_baseline(account, list(reversed([post.post_id for post in posts])))
                        await asyncio.to_thread(state_store.save)
                        self.ctx.logger.info("已为 @%s 建立基线，共记录 %d 条推文", account, len(posts))
                        continue
                    state_store.mark_baseline(account, [])
                    await asyncio.to_thread(state_store.save)

                pending_posts = [post for post in posts if not state_store.is_seen(account, post.post_id)]
                batch_size = self.config.nitter.max_posts_per_scan
                batch = list(reversed(pending_posts[-batch_size:]))
                for post in batch:
                    if post.is_retweet and not self.config.nitter.include_retweets:
                        state_store.mark_seen(account, post.post_id)
                        await asyncio.to_thread(state_store.save)
                        self.ctx.logger.info("已按配置跳过 @%s 的转推 %s", account, post.post_id)
                        continue
                    try:
                        prepared_post = await self._prepare_post(
                            client,
                            post,
                            tolerate_media_errors=False,
                        )
                    except Exception:
                        self.ctx.logger.error(
                            "准备 @%s 的推文 %s 正文或媒体失败",
                            account,
                            post.post_id,
                            exc_info=True,
                        )
                        continue
                    pending_deliveries[(prepared_post.account, prepared_post.post_id)] = (
                        prepared_post,
                        qq_groups,
                    )

            await self._update_subscription_display_names(fetched_profile_names)

            posts_by_group: Dict[str, List[NitterPost]] = {}
            for post, qq_groups in pending_deliveries.values():
                for group_id in qq_groups:
                    posts_by_group.setdefault(group_id, []).append(post)

            for group_id, group_posts in posts_by_group.items():
                group_posts.sort(key=lambda post: post.published_at)
                incomplete_posts = [
                    post
                    for post in group_posts
                    if not self._post_group_delivery_completed(post, group_id)
                ]
                if not incomplete_posts:
                    continue

                try:
                    stream_id = await self._resolve_group_stream(group_id, stream_cache)
                except Exception:
                    self.ctx.logger.error("无法打开 QQ 群 %s 的聊天流", group_id, exc_info=True)
                    continue

                clean_posts = [
                    post
                    for post in incomplete_posts
                    if not state_store.completed_tokens(post.account, post.post_id, group_id)
                ]
                individual_posts = incomplete_posts
                if len(clean_posts) > self.config.delivery.forward_batch_threshold:
                    individual_posts = [post for post in incomplete_posts if post not in clean_posts]
                    try:
                        forward_sent = await self._send_posts_forward(
                            client,
                            clean_posts,
                            stream_id,
                            tolerate_media_errors=False,
                        )
                        if not forward_sent:
                            raise RuntimeError(f"向 QQ 群 {group_id} 发送推文合并转发失败")
                        for post in clean_posts:
                            await self._record_delivery_token(post, group_id, FORWARD_TOKEN)
                    except Exception:
                        self.ctx.logger.error(
                            "向 QQ 群 %s 合并转发 %d 条推文失败",
                            group_id,
                            len(clean_posts),
                            exc_info=True,
                        )

                for post in individual_posts:
                    try:
                        await self._deliver_post_to_group(client, post, group_id, stream_id)
                    except Exception:
                        self.ctx.logger.error(
                            "推文 %s 投递到 QQ 群 %s 失败",
                            post.post_id,
                            group_id,
                            exc_info=True,
                        )

            for post, qq_groups in pending_deliveries.values():
                if not all(self._post_group_delivery_completed(post, group_id) for group_id in qq_groups):
                    continue
                state_store.mark_seen(post.account, post.post_id)
                await asyncio.to_thread(state_store.save)
                summary.forwarded_posts += 1
                self.ctx.logger.info("已转发 @%s 的推文 %s", post.account, post.post_id)

            return summary

    async def _deliver_post_to_group(
        self,
        client: NitterClient,
        post: NitterPost,
        group_id: str,
        stream_id: str,
    ) -> None:
        """把一条推文按投递进度发送到一个目标群。"""

        message_text = self._format_post_text(post)
        selected_media = self._select_media(post.media)
        media_cache: Dict[str, CachedMedia] = {}
        completed_tokens = self._require_state_store().completed_tokens(
            post.account,
            post.post_id,
            group_id,
        )

        if FORWARD_TOKEN in completed_tokens:
            return
        if MESSAGE_TOKEN not in completed_tokens:
            message_sent = await self._send_post_message(stream_id, message_text)
            if not message_sent:
                raise RuntimeError(f"向 QQ 群 {group_id} 发送推文正文失败")
            await self._record_delivery_token(post, group_id, MESSAGE_TOKEN)
            completed_tokens.add(MESSAGE_TOKEN)

        for index, media in enumerate(selected_media, start=1):
            media_token = self._media_token(media)
            if media_token in completed_tokens:
                continue
            media_sent = await self._send_media_attachment(
                client,
                post,
                media,
                index,
                stream_id,
                media_cache,
            )
            if not media_sent:
                raise RuntimeError(f"向 QQ 群 {group_id} 发送推文附件失败: {media.url}")
            await self._record_delivery_token(post, group_id, media_token)
            completed_tokens.add(media_token)

    def _post_group_delivery_completed(self, post: NitterPost, group_id: str) -> bool:
        """判断推文在目标群中是否已经完整发送。"""

        completed_tokens = self._require_state_store().completed_tokens(
            post.account,
            post.post_id,
            group_id,
        )
        if FORWARD_TOKEN in completed_tokens:
            return True
        required_tokens = {MESSAGE_TOKEN}
        required_tokens.update(self._media_token(media) for media in self._select_media(post.media))
        return required_tokens.issubset(completed_tokens)

    async def _resolve_group_stream(self, group_id: str, stream_cache: Dict[str, str]) -> str:
        """通过 SDK 打开真实 QQ 群聊天流，不自行计算 session_id。"""

        if group_id in stream_cache:
            return stream_cache[group_id]
        session = await self.ctx.chat.open_session(
            platform="qq",
            chat_type="group",
            group_id=group_id,
            account_id=self.config.delivery.qq_account_id,
        )
        if not isinstance(session, dict) or not session.get("success"):
            error = session.get("error", "未知错误") if isinstance(session, dict) else str(session)
            raise RuntimeError(f"无法打开 QQ 群 {group_id} 的聊天流: {error}")
        stream_id = str(session.get("stream_id") or session.get("session_id") or "").strip()
        if not stream_id:
            raise RuntimeError(f"QQ 群 {group_id} 的聊天流响应缺少 stream_id")
        stream_cache[group_id] = stream_id
        return stream_id

    async def _cache_media(
        self,
        client: NitterClient,
        media: MediaAttachment,
        file_name: str,
        media_cache: Dict[str, CachedMedia],
    ) -> CachedMedia:
        """把媒体下载到插件缓存，并在同一轮多群投递间复用。"""

        if media.url not in media_cache:
            max_bytes = self.config.delivery.max_media_size_mb * 1024 * 1024
            media_cache[media.url] = await self._require_media_cache_service().cache_media(
                media.url,
                file_name,
                max_bytes,
                client.download_media_to_file,
            )
        return media_cache[media.url]

    async def _send_post_message(self, stream_id: str, message_text: str) -> bool:
        """发送推文正文。"""

        return bool(await self.ctx.send.text(message_text, stream_id))

    async def _send_media_attachment(
        self,
        client: NitterClient,
        post: NitterPost,
        media: MediaAttachment,
        index: int,
        stream_id: str,
        media_cache: Dict[str, CachedMedia],
    ) -> bool:
        """先缓存单个媒体；小图片走 Base64，视频和大图使用缓存 URL。"""

        content_type = self._media_content_type(media)
        file_name = self._build_media_filename(post, media, index, content_type)
        try:
            cached_media = await self._cache_media(client, media, file_name, media_cache)
        except (MediaTooLargeError, MediaCacheLimitError):
            self.ctx.logger.warning("推文媒体超过缓存下载上限，改用原始 URL 文件消息: %s", media.url)
            media_url = media.url
        else:
            content_type = cached_media.content_type
            media_url = cached_media.public_url
            if media.media_type == "image" and cached_media.size <= MAX_INLINE_MEDIA_BYTES:
                media_data = await asyncio.to_thread(cached_media.path.read_bytes)
                return bool(
                    await self.ctx.send.image(
                        base64.b64encode(media_data).decode("ascii"),
                        stream_id,
                        processed_plain_text="[推文图片]",
                    )
                )
            if media.media_type == "image":
                self.ctx.logger.warning("推文图片超过 10 MiB 内嵌安全上限，改用缓存文件消息: %s", media.url)

        return bool(
            await self.ctx.send.custom(
                "file",
                {
                    "name": self._build_media_filename(post, media, index, content_type),
                    "mime_type": content_type,
                    "url": media_url,
                },
                stream_id,
                processed_plain_text="[推文附件]",
            )
        )

    async def _record_delivery_token(self, post: NitterPost, group_id: str, token: str) -> None:
        """立即持久化单个发送步骤，减少失败重试时的重复消息。"""

        state_store = self._require_state_store()
        state_store.mark_token_completed(post.account, post.post_id, group_id, token)
        await asyncio.to_thread(state_store.save)

    def _select_media(self, media_items: List[MediaAttachment]) -> List[MediaAttachment]:
        """按照附件开关筛选媒体。"""

        selected: List[MediaAttachment] = []
        for media in media_items:
            if media.media_type == "image" and self.config.delivery.send_images:
                selected.append(media)
            elif media.media_type == "video" and self.config.delivery.send_videos:
                selected.append(media)
            elif media.media_type == "file" and self.config.delivery.send_other_files:
                selected.append(media)
        return selected

    @staticmethod
    def _media_content_type(media: MediaAttachment) -> str:
        """根据附件声明和 URL 推断文件 MIME 类型。"""

        return (
            media.mime_type
            or mimetypes.guess_type(urlsplit(media.url).path)[0]
            or "application/octet-stream"
        )

    @staticmethod
    def _format_post_text(post: NitterPost) -> str:
        """生成发送到群中的简体中文推文正文。"""

        beijing_time = post.published_at.astimezone(BEIJING_TIMEZONE).strftime("%Y-%m-%d %H:%M")
        author = post.author if post.author.startswith("@") else f"@{post.author}"
        source_account = author.lstrip("@")
        if not USERNAME_PATTERN.fullmatch(source_account):
            source_account = post.account
        source_url = f"{OFFICIAL_STATUS_BASE_URL}/{source_account}/status/{post.post_id}"
        if post.is_retweet:
            headline = f"@{post.account} 转推了 {author} · {beijing_time}（北京时间）"
        else:
            headline = f"@{post.account} · {beijing_time}（北京时间）"
        lines = [headline, "", post.text]
        if post.translated_text:
            lines.extend(["", "中文翻译：", post.translated_text])
        if post.has_video and not any(media.media_type == "video" for media in post.media):
            lines.extend(["", "媒体提示：原推文包含视频，当前 Nitter 实例未提供可下载地址"])
        lines.extend(["", f"原文：{source_url}"])
        return "\n".join(lines)

    @staticmethod
    def _media_token(media: MediaAttachment) -> str:
        """为附件生成稳定的投递进度标识。"""

        return f"media:{sha256(media.url.encode('utf-8')).hexdigest()}"

    @staticmethod
    def _build_media_filename(
        post: NitterPost,
        media: MediaAttachment,
        index: int,
        content_type: str,
    ) -> str:
        """为 QQ 群文件生成可读且安全的文件名。"""

        decoded_path = unquote(urlsplit(media.url).path)
        raw_suffix = Path(decoded_path.split("?", maxsplit=1)[0]).suffix.lower()
        guessed_suffix = mimetypes.guess_extension(content_type) or ""
        suffix = raw_suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,8}", raw_suffix) else guessed_suffix
        if suffix == ".jpe":
            suffix = ".jpg"
        if not suffix:
            suffix = ".mp4" if media.media_type == "video" else ".bin"
        return f"tweet_{post.post_id}_{index}{suffix}"

    def _require_state_store(self) -> StateStore:
        """返回已初始化的状态存储。"""

        if self._state_store is None:
            raise RuntimeError("NitterToMaiBot 状态存储尚未初始化")
        return self._state_store

    def _require_media_cache_service(self) -> MediaCacheService:
        """返回已经初始化的媒体缓存服务。"""

        if self._media_cache_service is None:
            raise RuntimeError("NitterToMaiBot 媒体缓存服务尚未初始化")
        return self._media_cache_service

    async def _sync_subscription_mirror(self) -> None:
        """把真实订阅快照写入后台只读展示，并移除旧全局订阅字段。"""

        snapshot = self._require_subscription_store().snapshot()
        changed = await asyncio.to_thread(self._config_mirror.sync, snapshot)
        if changed:
            self.ctx.logger.info("NitterToMaiBot 后台只读订阅列表已同步")

    async def _update_subscription_display_names(self, display_names: Dict[str, str]) -> None:
        """批量保存 RSS 中解析到的账号显示名，并同步后台只读镜像。"""

        if not display_names:
            return
        subscription_store = self._require_subscription_store()
        async with self._subscription_lock:
            changed = False
            for account, display_name in display_names.items():
                if subscription_store.set_display_name(account, display_name):
                    changed = True
            if not changed:
                return
            await asyncio.to_thread(subscription_store.save)
            await self._sync_subscription_mirror()

    def _require_subscription_store(self) -> SubscriptionStore:
        """返回已初始化的统一订阅存储。"""

        if self._subscription_store is None:
            raise RuntimeError("NitterToMaiBot 订阅存储尚未初始化")
        return self._subscription_store


def create_plugin() -> NitterToMaiBotPlugin:
    """创建插件实例。"""

    return NitterToMaiBotPlugin()
