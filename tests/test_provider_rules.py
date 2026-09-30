"""The model-specific rules (brain.kimi_model_problems and its helpers) with OpenRouter's "vendor/model"
names: every Kimi rule is keyed on the model's base name ("moonshotai/kimi-k3" -> kimi-k3), non-Kimi models
get no false problems, and brain.web_search is a kimi-k3 problem only where it means Moonshot's builtin
$web_search tool loop (OpenRouter searches with its web plugin). Offline: no network, no key."""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bitpin.brain import (MIN_THINKING_MAX_TOKENS, check_kimi_config, is_no_web_search_model,  # noqa: E402
                          kimi_model_problems, model_base, uses_builtin_web_search)
from bitpin.llm import OPENROUTER_BASE_URL, validate_llm_config  # noqa: E402

RELAY = "https://relay.example.com/v1"
OR_LLM = {"base_url": OPENROUTER_BASE_URL, "api_key_env": "OPENROUTER_API_KEY"}
NON_KIMI = ("openai/gpt-5", "anthropic/claude-sonnet-4.5", "deepseek/deepseek-chat", "google/gemini-2.5-pro",
            "x-ai/grok-4", "moonshotai/kimi-k2.6", "vendor/not-kimi-k3")
FULL = {"risk_profile": "full"}
KIMI_CFG = {"news": {}, "context": {}}          # a news section, no endgame: no model-independent warnings


def llm(model, **kw):
    """A VALIDATED OpenRouter llm section (thinking defaults applied), as kimi_model_problems gets it."""
    return validate_llm_config(dict(OR_LLM, model=model, **kw))


class TestModelBaseName(unittest.TestCase):
    def test_the_vendor_prefix_is_stripped(self):
        for m, want in (("moonshotai/kimi-k3", "kimi-k3"), ("MoonshotAI/Kimi-K3", "kimi-k3"), ("kimi-k3", "kimi-k3"),
                        (" moonshotai/kimi-k3:free ", "kimi-k3:free"), ("a/b/kimi-k3", "kimi-k3"),
                        ("openai/gpt-5", "gpt-5"), ("", ""), (None, ""), (5, "")):
            self.assertEqual(model_base(m), want, m)

    def test_the_no_web_search_rule_follows_the_base_name(self):
        for m in ("kimi-k3", "moonshotai/kimi-k3", "MoonshotAI/Kimi-K3:free", "kimi-k3-preview"):
            self.assertTrue(is_no_web_search_model(m), m)
        for m in NON_KIMI + ("kimi-k2.6", "", None):
            self.assertFalse(is_no_web_search_model(m), m)

    def test_builtin_web_search_everywhere_but_openrouter(self):
        self.assertFalse(uses_builtin_web_search(llm("openai/gpt-5")))
        self.assertFalse(uses_builtin_web_search({"base_url": RELAY, "provider": "openrouter"}))
        for section in ({}, None, {"base_url": "https://api.moonshot.cn/v1"}, {"base_url": RELAY},
                        {"base_url": RELAY, "provider": "moonshot"}, {"provider": "nonsense"}):
            self.assertTrue(uses_builtin_web_search(section), section)


class TestKimiRulesWithVendorPrefix(unittest.TestCase):
    def test_openrouter_kimi_k3_gets_the_kimi_k3_rules(self):
        ok = llm("moonshotai/kimi-k3")                      # the thinking defaults: no temperature, 32000 tokens
        self.assertIsNone(ok["temperature"])
        self.assertGreaterEqual(ok["max_tokens"], MIN_THINKING_MAX_TOKENS)
        self.assertEqual(kimi_model_problems(ok, FULL, KIMI_CFG), ([], []))
        for kw, want in (({"temperature": 0.3}, "llm.temperature must be null for moonshotai/kimi-k3"),
                         ({"max_tokens": 4096}, "too small for the thinking model moonshotai/kimi-k3"),
                         ({"max_tokens": None}, "too small for the thinking model")):
            problems, warnings = kimi_model_problems(llm("moonshotai/kimi-k3", **kw), FULL, KIMI_CFG)
            self.assertEqual(len(problems), 1, problems)
            self.assertIn(want, problems[0])
            self.assertEqual(warnings, [])

    def test_web_search_is_a_kimi_k3_problem_only_with_the_builtin_tool(self):
        web = dict(FULL, web_search=True)
        # OpenRouter: web search is the web plugin (one request, no tool round) - fine for kimi-k3 too
        self.assertEqual(kimi_model_problems(llm("moonshotai/kimi-k3"), web, KIMI_CFG), ([], []))
        # a relay host served like Moonshot, and Moonshot itself: the builtin tool loop fails on kimi-k3
        relay = validate_llm_config({"base_url": RELAY, "model": "moonshotai/kimi-k3"})
        moon = validate_llm_config({"model": "kimi-k3"})
        for cfg in (relay, moon):
            problems, _ = kimi_model_problems(cfg, web, KIMI_CFG)
            self.assertEqual(len(problems), 1, problems)
            self.assertIn("brain.web_search must be false for", problems[0])
        # the rule set used before providers existed: a bare llm section is Moonshot
        p, w = kimi_model_problems({"model": "kimi-k3", "temperature": None, "max_tokens": 32000}, web, {"news": {}})
        self.assertEqual((len(p), w), (1, []))

    def test_other_models_get_no_false_problems(self):
        for m in NON_KIMI:
            for kw in ({}, {"temperature": 0.3, "max_tokens": 4096}, {"temperature": None, "max_tokens": None},
                       {"temperature": 1.0, "max_tokens": 1000}):
                cfg = llm(m, **kw)
                for brain in (FULL, dict(FULL, web_search=True)):
                    self.assertEqual(kimi_model_problems(cfg, brain, KIMI_CFG), ([], []), (m, kw, brain))
            # and on a relay host with Moonshot's tool loop: web_search is only a kimi-k3 problem
            relay = validate_llm_config({"base_url": RELAY, "model": m, "api_key_env": "OPENAI_API_KEY"})
            self.assertEqual(kimi_model_problems(relay, dict(FULL, web_search=True), KIMI_CFG), ([], []), m)

    def test_other_models_keep_temperature_and_max_tokens_as_configured(self):
        for m in NON_KIMI:
            cfg = llm(m)
            self.assertEqual((cfg["temperature"], cfg["max_tokens"]), (0.3, 4096), m)
            cfg = llm(m, temperature=0.9, max_tokens=20000)
            self.assertEqual((cfg["temperature"], cfg["max_tokens"]), (0.9, 20000), m)

    def test_the_model_independent_warnings_are_unchanged(self):
        _, warnings = kimi_model_problems(llm("openai/gpt-5"), {"risk_profile": "balanced"}, {})
        self.assertEqual(len(warnings), 2, warnings)
        self.assertIn("WITHOUT news", warnings[0])
        self.assertIn("risk_profile", warnings[1])


class TestCheckKimiConfigOnOpenRouter(unittest.TestCase):
    """check_kimi_config (what `bitpin-bot check` runs) on an OpenRouter kimi.json: offline, no key needed."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="provider_rules_")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def check(self, cfg):
        path = os.path.join(self.dir, "kimi.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        warnings = []
        return check_kimi_config(path, warnings=warnings), warnings

    def cfg(self, model, news_model="openai/gpt-5", **llm_kw):
        return {"llm": dict(OR_LLM, model=model, **llm_kw), "brain": {"risk_profile": "full"},
                "news": dict(OR_LLM, model=news_model)}

    def test_a_non_kimi_openrouter_config_passes(self):
        for m in ("openai/gpt-5", "anthropic/claude-sonnet-4.5", "deepseek/deepseek-chat"):
            self.assertEqual(self.check(self.cfg(m, temperature=0.3, max_tokens=4096)), ([], []), m)

    def test_openrouter_kimi_k3_is_checked_like_kimi_k3(self):
        self.assertEqual(self.check(self.cfg("moonshotai/kimi-k3")), ([], []))
        problems, _ = self.check(self.cfg("moonshotai/kimi-k3", temperature=0.3))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("llm.temperature must be null for moonshotai/kimi-k3", problems[0])
        # the news stage on OpenRouter searches with the web plugin: kimi-k3 is no problem there
        self.assertEqual(self.check(self.cfg("moonshotai/kimi-k3", news_model="moonshotai/kimi-k3")), ([], []))

    def test_a_key_variable_of_the_wrong_platform_fails_the_check(self):
        cfg = self.cfg("openai/gpt-5")
        cfg["llm"]["api_key_env"] = "KIMI_API_KEY"                   # the Kimi key would go to OpenRouter
        problems, _ = self.check(cfg)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("OPENROUTER_", problems[0])
        cfg = self.cfg("openai/gpt-5")
        del cfg["news"]["api_key_env"]                               # the news default KIMI_API_KEY: refused too
        cfg["news"]["base_url"] = OPENROUTER_BASE_URL
        cfg["llm"]["base_url"] = "https://api.moonshot.ai/v1"
        cfg["llm"]["api_key_env"] = "KIMI_API_KEY"
        cfg["llm"]["model"] = "kimi-k3"
        problems, _ = self.check(cfg)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("news.api_key_env", problems[0])


if __name__ == "__main__":
    unittest.main()
