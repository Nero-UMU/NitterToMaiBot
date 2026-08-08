"""插件 SDK 契约与配置测试。"""

from pathlib import Path
from unittest import TestCase

import json

from plugins.NitterToMaiBot.plugin import (
    DeliverySectionConfig,
    NitterSectionConfig,
    NitterToMaiBotConfig,
    PLUGIN_ID,
    QuietHoursSectionConfig,
    TRANSLATION_SYSTEM_PROMPT,
    TranslationSectionConfig,
    create_plugin,
)


class PluginContractTests(TestCase):
    """验证入口工厂、组件声明和配置校验。"""

    def test_create_plugin_and_commands(self) -> None:
        plugin = create_plugin()
        components = plugin.get_components()
        component_names = {component["name"] for component in components}

        self.assertIn("nitter_to_maibot_help", component_names)
        self.assertIn("nitter_to_maibot_status", component_names)
        self.assertIn("nitter_to_maibot_scan", component_names)
        self.assertIn("nitter_to_maibot_follow", component_names)
        self.assertIn("nitter_to_maibot_unfollow", component_names)
        self.assertIn("nitter_to_maibot_list_follows", component_names)
        self.assertIn("nitter_to_maibot_toggle_push", component_names)
        self.assertIn("nitter_to_maibot_posts", component_names)
        self.assertNotIn("nitter_to_maibot_test_account", component_names)
        self.assertIn("nitter_to_maibot_parse_status", component_names)
        self.assertIn("nitter_to_maibot_auto_parse_status", component_names)
        posts_component = next(
            component for component in components if component["name"] == "nitter_to_maibot_posts"
        )
        help_component = next(
            component for component in components if component["name"] == "nitter_to_maibot_help"
        )
        self.assertIn("twitter_help", help_component["metadata"]["command_pattern"])
        self.assertIn("twitter_posts", posts_component["metadata"]["command_pattern"])
        auto_parse_component = next(
            component for component in components if component["name"] == "nitter_to_maibot_auto_parse_status"
        )
        self.assertEqual(auto_parse_component["metadata"]["mode"], "blocking")

    def test_account_normalization(self) -> None:
        config = NitterSectionConfig(accounts=["@OpenAI", "openai"])

        self.assertEqual(config.accounts, ["OpenAI"])

    def test_invalid_group_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            DeliverySectionConfig(qq_groups=["群123"])

    def test_forward_batch_threshold_defaults_to_one(self) -> None:
        config = DeliverySectionConfig()

        self.assertEqual(config.forward_batch_threshold, 1)

    def test_translation_defaults_to_disabled_utils_task(self) -> None:
        config = TranslationSectionConfig()

        self.assertFalse(config.enabled)
        self.assertEqual(config.model, "utils")
        self.assertEqual(config.prompt, TRANSLATION_SYSTEM_PROMPT)

    def test_quiet_hours_defaults_and_validation(self) -> None:
        config = QuietHoursSectionConfig(start_time="0:00", end_time="6:00")

        self.assertFalse(config.enabled)
        self.assertEqual(config.start_time, "00:00")
        self.assertEqual(config.end_time, "06:00")
        with self.assertRaises(ValueError):
            QuietHoursSectionConfig(start_time="24:00")
        with self.assertRaises(ValueError):
            QuietHoursSectionConfig(start_time="06:00", end_time="06:00")

    def test_manifest_declares_forward_capability(self) -> None:
        manifest_path = Path(__file__).parents[1] / "_manifest.json"
        with manifest_path.open("r", encoding="utf-8") as manifest_file:
            manifest = json.load(manifest_file)

        self.assertIn("send.forward", manifest["capabilities"])
        self.assertIn("llm.generate", manifest["capabilities"])
        self.assertNotIn("config.get", manifest["capabilities"])
        self.assertEqual(manifest["id"], PLUGIN_ID)
        self.assertEqual(manifest["plugin_type"], "integration")
        self.assertEqual(
            manifest["urls"]["issues"],
            "https://github.com/Nero-UMU/NitterToMaiBot/issues",
        )

    def test_project_uses_gpl_3_or_later(self) -> None:
        project_dir = Path(__file__).parents[1]
        with (project_dir / "_manifest.json").open("r", encoding="utf-8") as manifest_file:
            manifest = json.load(manifest_file)
        license_text = (project_dir / "LICENSE").read_text(encoding="utf-8")

        self.assertEqual(manifest["license"], "GPL-3.0-or-later")
        self.assertIn("SPDX-License-Identifier: GPL-3.0-or-later", license_text)
        self.assertIn("GNU GENERAL PUBLIC LICENSE", license_text)
        self.assertIn("Version 3, 29 June 2007", license_text)

    def test_config_model_defaults(self) -> None:
        config = NitterToMaiBotConfig()
        self.assertEqual(config.nitter.base_url, "https://nitter.net")
        self.assertEqual(config.nitter.poll_interval_seconds, 600)
        self.assertEqual(config.delivery.forward_batch_threshold, 1)
        self.assertEqual(config.plugin.config_version, "1.5.3")
        self.assertFalse(config.translation.enabled)
        self.assertEqual(config.translation.model, "utils")
        self.assertEqual(config.translation.prompt, TRANSLATION_SYSTEM_PROMPT)
        self.assertEqual(config.delivery.max_media_size_mb, 10)
        self.assertFalse(config.quiet_hours.enabled)
        self.assertEqual(config.quiet_hours.start_time, "00:00")
        self.assertEqual(config.quiet_hours.end_time, "06:00")
        self.assertEqual(config.interaction.max_accounts_per_group, 0)
        self.assertTrue(config.interaction.auto_parse_tweet_links)

    def test_runtime_config_is_ignored(self) -> None:
        gitignore_path = Path(__file__).parents[1] / ".gitignore"
        ignored_patterns = gitignore_path.read_text(encoding="utf-8").splitlines()

        self.assertIn("/config.toml", ignored_patterns)

    def test_subscription_lists_are_read_only_in_schema(self) -> None:
        schema = create_plugin().build_config_schema()
        sections = schema["sections"]

        self.assertTrue(sections["nitter"]["fields"]["accounts"]["hidden"])
        self.assertTrue(sections["delivery"]["fields"]["qq_groups"]["hidden"])
        self.assertTrue(sections["subscriptions"]["fields"]["groups"]["disabled"])
        self.assertTrue(sections["subscriptions"]["fields"]["accounts"]["disabled"])

    def test_visible_config_fields_have_chinese_labels_and_explanations(self) -> None:
        schema = create_plugin().build_config_schema()

        for section in schema["sections"].values():
            for field_name, field_schema in section["fields"].items():
                if field_schema["hidden"]:
                    continue
                self.assertNotEqual(field_schema["label"], field_name)
                self.assertTrue(field_schema["description"])
                self.assertTrue(field_schema["hint"])

        qq_account_field = schema["sections"]["delivery"]["fields"]["qq_account_id"]
        self.assertEqual(qq_account_field["label"], "发送机器人 QQ 号")
        self.assertIn("不是目标群号", qq_account_field["hint"])
        self.assertEqual(qq_account_field["placeholder"], "例如：123456")
        auto_parse_field = schema["sections"]["interaction"]["fields"]["auto_parse_tweet_links"]
        self.assertEqual(auto_parse_field["label"], "自动解析推文链接")
        nitter_url_field = schema["sections"]["nitter"]["fields"]["base_url"]
        self.assertEqual(nitter_url_field["default"], "https://nitter.net")
        self.assertEqual(nitter_url_field["placeholder"], "https://nitter.net")
        include_retweets_field = schema["sections"]["nitter"]["fields"]["include_retweets"]
        self.assertEqual(include_retweets_field["label"], "转发转推")
        self.assertIn("/推特推文", include_retweets_field["description"])
        self.assertIn("/推特解析不受此选项影响", include_retweets_field["hint"])
        translation_fields = schema["sections"]["translation"]["fields"]
        self.assertEqual(translation_fields["enabled"]["label"], "启用推文翻译")
        self.assertEqual(translation_fields["model"]["label"], "翻译模型")
        self.assertEqual(translation_fields["model"]["choices"], ["utils", "replyer", "planner"])
        self.assertEqual(translation_fields["prompt"]["label"], "翻译提示词")
        self.assertEqual(translation_fields["prompt"]["default"], TRANSLATION_SYSTEM_PROMPT)
        self.assertEqual(translation_fields["prompt"]["ui_type"], "textarea")
        quiet_fields = schema["sections"]["quiet_hours"]["fields"]
        self.assertEqual(quiet_fields["enabled"]["label"], "启用静默时段")
        self.assertEqual(quiet_fields["start_time"]["default"], "00:00")
        self.assertEqual(quiet_fields["end_time"]["default"], "06:00")
