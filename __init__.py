# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import os
import secrets
from typing import Any

from ovos_bus_client.session import SessionManager, Session
from ovos_utils import classproperty
from ovos_utils.gui import can_use_gui
from ovos_utils.log import LOG
from ovos_utils.process_utils import RuntimeRequirements
from ovos_workshop.decorators import intent_handler
from ovos_workshop.skills.common_query_skill import CommonQuerySkill, CQSMatchLevel
from padacioso import IntentContainer
from padacioso.bracket_expansion import expand_parentheses
from stability_ai_api.basic_types import EngineIdV1
from stability_ai_api.stability_ai_api import StabilityAiV1Solver


MODEL_MAPPING = {
    "sdxl_v1.0": EngineIdV1.SDXL_10,
    "sd_v1.6": EngineIdV1.SD_16,
    "sd_beta": EngineIdV1.SD_BETA,
}

DEFAULT_SETTINGS = {
    "style_preset": "photographic",
    "model": "sdxl_v1.0",
}

IMAGE_WIDTH = 1216
IMAGE_HEIGHT = 832


class StabilityAiKeywordHandler:
    priority = 40
    enable_tx = False
    kw_matchers = {}

    # utils to extract keyword from text
    @classmethod
    def register_kw_extractors(cls, samples: list, lang: str) -> None:
        lang = lang.split("-")[0]
        if lang not in cls.kw_matchers:
            cls.kw_matchers[lang] = IntentContainer()
        cls.kw_matchers[lang].add_intent("question", samples)

    @classmethod
    def extract_keyword(cls, utterance: str, lang: str) -> str:
        lang = lang.split("-")[0]
        if lang not in cls.kw_matchers:
            return None
        matcher: IntentContainer = cls.kw_matchers[lang]
        match = matcher.calc_intent(utterance)
        kw = match.get("entities", {}).get("keyword")
        if kw:
            LOG.debug(f"StabilityAI Keyword: {kw} - Confidence: {match['conf']}")
        else:
            LOG.debug(f"Could not extract search keyword for '{lang}' from '{utterance}'")
        return kw


class StabilityAiSkill(CommonQuerySkill):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.session_results = {}
        self.settings.merge(DEFAULT_SETTINGS, new_only=True)
        self.kw_handler = StabilityAiKeywordHandler()
        self.register_kw_xtract()

    def register_kw_xtract(self):
        """internal padacioso intents for kw extraction"""
        for lang in self.native_langs:
            filename = f"{self.root_dir}/locale/{lang}/query.intent"
            if not os.path.isfile(filename):
                LOG.warning(f"{filename} not found! StabilityAI will be disabled for '{lang}'")
                continue
            samples = []
            with open(filename) as f:
                for l in f.read().split("\n"):
                    if not l.strip() or l.startswith("#"):
                        continue
                    if "(" in l:
                        samples += expand_parentheses(l)
                    else:
                        samples.append(l)
            self.kw_handler.register_kw_extractors(samples, lang)

    @classproperty
    def runtime_requirements(self):
        """ indicate to OVOS this skill should ONLY
         be loaded if we have internet connection"""
        return RuntimeRequirements(
            internet_before_load=True,
            network_before_load=True,
            gui_before_load=False,
            requires_internet=True,
            requires_network=True,
            requires_gui=False,
            no_internet_fallback=False,
            no_network_fallback=False,
            no_gui_fallback=True,
        )

    @property
    def ai_name(self):
        return self.settings.get("name", "Stability AI")

    @property
    def confirmation(self):
        return self.settings.get("confirmation", True)

    @property
    def solver(self):
        """created fresh to allow key/url rotation when settings.json is edited"""
        try:
            return StabilityAiV1Solver(
                api_key=self.settings.get("api_key"),
                engine_id=MODEL_MAPPING.get(
                    self.settings.get("model")
                )
            )
        except Exception as err:
            self.log.error(err)
            return None

    # common query
    def CQS_match_query_phrase(self, phrase: str) -> Any:
        session = SessionManager.get()
        query = self.kw_handler.extract_keyword(phrase, session.lang)
        if not query:
            # doesnt look like a question we can answer at all
            return None
        title = "Stability AI"
        self.session_results[session.session_id] = {
            "query": query,
            "idx": 0,
            "lang": session.lang,
            "title": title,
            "image": None
        }
        success = self.ask_stability_ai(session)
        if success:
            self.log.info("Stability AI answered")
            self.session_results[session.session_id]["idx"] += 1  # spoken by common query
            self.session_results[session.session_id]["title"] = title
            return (
                phrase,
                CQSMatchLevel.GENERAL,
                "done",
                {
                    "query": query,
                    "image": self.session_results[session.session_id].get("image"),
                    "title": title,
                    "answer": phrase
                },
            )

    def CQS_action(self, phrase: str, data: Any) -> None:
        """If selected show GUI"""
        session = SessionManager.get()
        if session.session_id in self.session_results:
            self.show_result(session)
        else:
            LOG.error(f"{session.session_id} not in "
                      f"{list(self.session_results.keys())}")

        self.set_context("StabilityAIDraws", data.get("title") or phrase)

    # intents
    @intent_handler("stability_ai.intent")
    @intent_handler("query.intent")
    def handle_query(self, message):
        """Handle query."""
        query = message.data["query"]

        session = SessionManager.get(message)
        self.session_results[session.session_id] = {
            "query": query,
            "idx": 0,
            "lang": session.lang,
            "image": None,
        }

        if self.confirmation:
            self.speak_dialog("asking", data={"name": self.ai_name})

        success = self.ask_stability_ai(session)
        if success:
            self.show_result(session)
        else:
            self.speak_dialog("error", data={"name": self.ai_name})

    # Stability AI
    def ask_stability_ai(self, session: Session):
        query = self.session_results[session.session_id]["query"]

        if "api_key" not in self.settings:
            self.log.error(
                "Stability AI not configured yet, please set your API key: %s",
                self.settings.path,
            )
            return False  # StabilityAI not configured yet

        try:
            image = self.solver.tti_query(  # text-to-image query
                prompts={"text": query},
                width=IMAGE_WIDTH,
                height=IMAGE_HEIGHT,
                style_preset=self.settings.get("style_preset")
            )
            # TODO: use system cache
            result = os.path.expanduser(f"~/.cache/figure_{secrets.token_urlsafe(16)}.png")
            with open(result, 'wb') as f:
                f.write(image)
        except Exception as err:  # handle solver plugin failures, happens in some queries
            self.log.error(err)
            result = None

        if result:
            self.session_results[session.session_id]["image"] = result
            return True
        return False

    def display_result(self, title: str):
        if not can_use_gui(self.bus):
            LOG.debug(f"GUI not enabled")
            return
        session = SessionManager.get()
        image = self.session_results[session.session_id].get("image")
        if image:
            self.session_results[session.session_id]["image"] = image
            self.gui.show_image(
                image, title=title, fill='PreserveAspectFit',
                override_idle=60, override_animations=True
            )
        else:
            LOG.info(f"no image in {self.session_results[session.session_id]}")

    def show_result(self, session: Session):
        if session.session_id in self.session_results:
            title = self.session_results[session.session_id].get("title") or \
                "StabilityAI"
            self.speak_dialog("done")
            self.display_result(title)
            self.session_results[session.session_id]["idx"] += 1
        else:
            self.speak_dialog("none")

    def stop(self):
        self.gui.release()

    def stop_session(self, session):
        if session.session_id in self.session_results:
            self.session_results.pop(session.session_id)


if __name__ == "__main__":
    from ovos_utils.fakebus import FakeBus

    s = StabilityAiSkill(bus=FakeBus(), skill_id="stability_ai.skill")
    print(s.CQS_match_query_phrase("draw me a cat wearing a hat"))