import unittest
from ovos_plugin_manager.skills import find_skill_plugins
from ovos_skill_stability_ai import StabilityAiSkill


class TestPlugin(unittest.TestCase):
    def test_skill_id(self):
        setup_skill_id = "skill-ovos-stability-ai.femelo"
        plugs = find_skill_plugins()
        self.assertTrue(setup_skill_id in plugs)
        self.assertEqual(plugs[setup_skill_id], StabilityAiSkill)
