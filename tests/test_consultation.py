"""The public turn finalizer must consume Engine's one deterministic ranking."""

from __future__ import annotations

import unittest
from dataclasses import asdict

from app.consultation import finalize_turn
from app.engine import Engine, Profile


class ConsultationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = Engine()
        cls.cafe = cls.engine.find_business("카페")

    def test_equivalent_phrasing_and_provider_output_use_the_same_engine_ids(self) -> None:
        left_profile = Profile(business=self.cafe, regionResolved=True)
        right_profile = Profile(business=self.engine.find_business("반찬"), regionResolved=True,
                                decisionPreference="subShare")
        left = finalize_turn(
            {"profile": asdict(left_profile), "agent": {"provider": "openai"},
             "candidates": ["00000000"], "candidateNames": ["모델 후보"], "legacyMarker": "kept"},
            "카페를 열 때 생활시설 주변을 더 중요하게 봐요.",
            left_profile,
        )
        right = finalize_turn(
            {"profile": asdict(right_profile), "agent": {"provider": "gemini"},
             "candidates": ["99999999", "99999998"], "candidateNames": ["다른 순위"],
             "message": "모델이 고른 순위입니다."},
            "커피 가게를 차릴 때 마트와 병원 같은 생활시설을 더 중요하게 생각합니다.",
            right_profile,
        )

        left_ids = [item["id"] for item in left["notebook"]["results"]]
        right_ids = [item["id"] for item in right["notebook"]["results"]]
        self.assertEqual(left_ids, right_ids)
        self.assertEqual(left["candidates"], right["candidates"])
        self.assertTrue(all(code in self.engine.by_code for code in left_ids))
        self.assertEqual(left["profile"]["business"], self.cafe)
        self.assertEqual(right["profile"]["business"], self.cafe)
        self.assertEqual(left["profile"]["decisionPreference"], "anchors")
        self.assertEqual(right["profile"]["decisionPreference"], "anchors")
        self.assertEqual(left["legacyMarker"], "kept")
        self.assertEqual(left["notebook"]["results"][0]["date"], self.engine.meta["storeDate"])
        self.assertIn("data/793", left["notebook"]["results"][0]["source"])

    def test_region_question_is_adaptive_and_uses_mapped_global_candidates(self) -> None:
        profile = Profile(business=self.cafe)
        out = finalize_turn({"profile": asdict(profile)}, "제주에서 작은 카페를 열고 싶어요", profile)

        notebook = out["notebook"]
        self.assertEqual(notebook["state"], "question")
        self.assertIn("지역", notebook["question"])
        self.assertEqual([choice["label"] for choice in notebook["choices"]], ["제주시", "서귀포시", "아직 모름"])
        self.assertTrue(notebook["results"])
        self.assertEqual(out["candidates"], [item["id"] for item in notebook["results"]])
        self.assertTrue(all(code in self.engine.by_code for code in out["candidates"]))

    def test_engine_ties_use_id_without_claiming_superiority_and_missing_is_not_zero(self) -> None:
        engine = Engine()
        engine.anchor_subs = [1]
        engine.dongs = [
            {"code": "00000003", "name": "표본동3", "sgg": "제주시", "total": None, "sub": {"0": 0, "1": 0}},
            {"code": "00000002", "name": "표본동2", "sgg": "제주시", "total": 0, "sub": {"0": 0, "1": 0}},
            {"code": "00000001", "name": "표본동1", "sgg": "제주시", "total": 10, "sub": {"0": 0, "1": 0}},
        ]
        engine.by_code = {dong["code"]: dong for dong in engine.dongs}
        profile = Profile(business=0, regionResolved=True, decisionPreference="anchors")

        tied_rows = engine.rank_consultation(profile, "anchors", limit=None)
        self.assertEqual([row["id"] for row in tied_rows], ["00000001", "00000002", "00000003"])
        out = engine.consultation_decision(profile, "")
        self.assertIn("동률", out["summary"])
        self.assertIn("우열을 뜻하지 않습니다", out["summary"])
        self.assertNotIn("더 우수", out["summary"])

        profile.decisionPreference = "subShare"
        share_rows = engine.rank_consultation(profile, "subShare", limit=None)
        by_id = {row["id"]: row for row in share_rows}
        self.assertEqual(by_id["00000001"]["share"], 0.0)
        self.assertIsNone(by_id["00000002"]["share"])
        self.assertEqual(by_id["00000002"]["denominator"], 0)
        self.assertIsNone(by_id["00000003"]["denominator"])
        results = engine.consultation_decision(profile, "")["results"]
        self.assertEqual([item["denominator"] for item in results], [10, 0, None])
        self.assertIn("0곳", results[1]["reason"])
        self.assertIn("분모가 확인되지 않아", results[2]["reason"])

    def test_only_explicit_must_conditions_filter_and_empty_pool_is_not_relaxed(self) -> None:
        preferred = self.engine.normalize_consultation_profile(
            Profile(business=self.cafe), "제주시 쪽이 더 편해요."
        )
        self.assertEqual(preferred.region, "제주시")
        self.assertNotIn("region", preferred.requiredConditions)

        required = self.engine.normalize_consultation_profile(
            Profile(business=self.cafe), "제주시에서만 카페를 하려고 해요."
        )
        self.assertIn("region", required.requiredConditions)
        regional_rows = self.engine.rank_consultation(required, "anchors", limit=None)
        self.assertTrue(regional_rows)
        self.assertTrue(all(self.engine.by_code[row["id"]]["sgg"] == "제주시" for row in regional_rows))

        engine = Engine()
        engine.dongs = [
            {"code": "00000001", "name": "표본동", "sgg": "제주시", "total": 10, "sub": {"0": 1}},
        ]
        engine.by_code = {dong["code"]: dong for dong in engine.dongs}
        profile = Profile(business=0, region="서귀포시", regionResolved=True,
                          decisionPreference="anchors", requiredConditions=["region"])
        decision = engine.consultation_decision(profile, "")
        self.assertEqual(decision["candidates"], [])
        self.assertEqual(decision["alternatives"], [])
        self.assertIn("서귀포시가 아님", decision["summary"])

    def test_area_relaxation_phrase_does_not_restore_previously_selected_area(self) -> None:
        profile = Profile(business=self.cafe, region="서귀포시", area="읍면")
        self.engine.apply_conditions(profile, {"area": "상관없음"})
        normalized = self.engine.normalize_consultation_profile(profile, "읍면 아니어도 괜찮아요")

        self.assertIsNone(normalized.area)
        self.assertNotIn("area", normalized.requiredConditions)

    def test_interpreted_preferences_are_not_implicitly_hard_filters(self) -> None:
        profile = Profile()
        self.engine.apply_conditions(profile, {
            "business": "카페",
            "region": "서귀포시",
            "area": "읍면",
            "competition": True,
        })
        text = "서귀포 읍면에서 카페를 하고 싶고 경쟁 적은 곳이 더 좋아요."
        profile = self.engine.normalize_consultation_profile(profile, text)
        self.assertEqual(profile.requiredConditions, [])

        out = finalize_turn({"profile": asdict(profile), "agent": {"provider": "gemini"}}, text, profile)
        self.assertTrue(out["notebook"]["results"])
        self.assertTrue(any(
            self.engine.by_code[item["id"]]["sgg"] != "서귀포시"
            or not self.engine.is_eup_myeon(self.engine.by_code[item["id"]])
            for item in out["notebook"]["results"]
        ))

    def test_explicit_model_interpreted_filters_survive_finalization(self) -> None:
        profile = Profile()
        self.engine.apply_conditions(profile, {
            "business": "카페",
            "region": "서귀포시",
            "area": "읍면",
            "competition": True,
        })
        text = "서귀포 시골에서만 카페를 하고 싶어요. 경쟁 적은 곳만 보여 주세요."
        profile = self.engine.normalize_consultation_profile(profile, text)
        self.assertTrue({"region", "area", "competition"} <= set(profile.requiredConditions))

        out = finalize_turn({"profile": asdict(profile), "agent": {"provider": "gemini"}}, text, profile)
        self.assertTrue(all(self.engine.by_code[item["id"]]["sgg"] == "서귀포시" for item in out["notebook"]["results"]))
        self.assertTrue(all(self.engine.is_eup_myeon(self.engine.by_code[item["id"]])
                            for item in out["notebook"]["results"]))
        self.assertEqual(out["candidates"], [item["id"] for item in out["notebook"]["results"]])
        if not out["notebook"]["results"]:
            self.assertEqual(out["alternatives"], [])
            self.assertIn("후보가 없습니다", out["notebook"]["summary"])

    def test_three_results_are_taken_after_required_region_filtering(self) -> None:
        profile = Profile(business=self.cafe, region="제주시", regionResolved=True,
                          decisionPreference="anchors", requiredConditions=["region"])
        filtered_order = self.engine.rank_consultation(profile, "anchors", limit=None)
        self.assertGreater(len(filtered_order), 3)

        out = finalize_turn({"profile": asdict(profile)}, "", profile)
        ids = [item["id"] for item in out["notebook"]["results"]]
        self.assertEqual(ids, [row["id"] for row in filtered_order[:3]])
        self.assertLessEqual(len(ids), 3)
        self.assertTrue(all(self.engine.by_code[code]["sgg"] == "제주시" for code in ids))

    def test_safe_model_sentence_survives_without_moving_the_local_ranking(self) -> None:
        profile = Profile(business=self.cafe, regionResolved=True, decisionPreference="anchors")
        text = "카페를 열 때 생활시설 주변을 더 중요하게 봐요."
        local = finalize_turn({"profile": asdict(profile)}, text, profile)
        reply = "말씀하신 생활 편의시설 기준으로 후보를 정리했어요."
        out = finalize_turn(
            {"profile": asdict(profile), "agent": {"used": True, "path": "model"}, "agentReply": reply,
             "candidates": ["00000000"], "candidateNames": ["모델 후보"]},
            text, profile,
        )

        self.assertEqual(out["agentReply"], reply)
        self.assertTrue(out["notebook"]["summary"].startswith(local["notebook"]["summary"]))
        self.assertIn(reply, out["notebook"]["summary"])
        # message stays local so the chat view does not print the provider sentence twice
        self.assertEqual(out["message"], local["message"])
        self.assertEqual(out["candidates"], local["candidates"])
        self.assertEqual(out["candidateNames"], local["candidateNames"])
        self.assertEqual([c["label"] for c in out["chips"]], [c["label"] for c in local["chips"]])

    def test_both_providers_keep_the_same_sentence_and_ranking(self) -> None:
        profile = Profile(business=self.cafe, regionResolved=True, decisionPreference="anchors")
        text = "카페를 열 때 생활시설 주변을 더 중요하게 봐요."
        reply = "말씀하신 생활 편의시설 기준으로 후보를 정리했어요."
        # Gemini's finish() reports used/calls/steps without a path key; OpenAI adds path="model".
        gemini = finalize_turn(
            {"profile": asdict(profile), "agent": {"used": True, "calls": [], "steps": []}, "agentReply": reply},
            text, profile,
        )
        openai = finalize_turn(
            {"profile": asdict(profile), "agent": {"used": True, "path": "model"}, "agentReply": reply},
            text, profile,
        )
        bare = finalize_turn({"profile": asdict(profile), "agentReply": reply}, text, profile)

        self.assertEqual(gemini["agentReply"], reply)
        self.assertEqual(openai["agentReply"], reply)
        self.assertEqual(gemini["candidates"], openai["candidates"])
        self.assertEqual(gemini["notebook"], openai["notebook"])
        # A payload that does not report a model turn carries no provider prose at all.
        self.assertEqual(bare["agentReply"], "")
        self.assertEqual(bare["candidates"], openai["candidates"])

    def test_model_sentence_is_dropped_when_local_evidence_does_not_support_it(self) -> None:
        profile = Profile(business=self.cafe, regionResolved=True, decisionPreference="anchors")
        text = "카페를 열 때 생활시설 주변을 더 중요하게 봐요."
        local = finalize_turn({"profile": asdict(profile)}, text, profile)
        for reply in (
            "생활시설 283곳 기준으로 골랐어요.",            # 근거 없는 수치
            "추자면과 우도면이 후보입니다.",                # 후보 밖 지명
            "매출이 클 가능성이 높습니다.",                  # 근거 없는 인과
            "노형동이 가장 좋습니다.",                      # 우열 주장
            "연동을 먼저 보세요.",                          # 엔진 1순위와 다른 순서
            "서귀포 쪽에서 골랐어요.",                      # 필터하지 않은 지역
            "말씀을 정리했어요. 지도에서 보세요. 바로 확인하세요.",  # 세 문장
            "생활시설 등록점포 수 순서로 표시했습니다.",     # 엔진 설명 중복
        ):
            with self.subTest(reply=reply):
                out = finalize_turn(
                    {"profile": asdict(profile), "agent": {"used": True, "path": "model"}, "agentReply": reply},
                    text, profile,
                )
                self.assertEqual(out["agentReply"], "")
                self.assertEqual(out["notebook"]["summary"], local["notebook"]["summary"])
                self.assertEqual(out["candidates"], local["candidates"])

    def test_two_sentence_reply_keeps_one_sentence_next_to_the_summary(self) -> None:
        profile = Profile(business=self.cafe, regionResolved=True, decisionPreference="anchors")
        text = "카페를 열 때 생활시설 주변을 더 중요하게 봐요."
        out = finalize_turn(
            {"profile": asdict(profile), "agent": {"used": True, "path": "model"},
             "agentReply": "말씀하신 기준으로 후보를 정리했어요. 지도에서 확인하세요."},
            text, profile,
        )

        self.assertEqual(out["agentReply"], "말씀하신 기준으로 후보를 정리했어요. 지도에서 확인하세요.")
        self.assertTrue(out["notebook"]["summary"].endswith("말씀하신 기준으로 후보를 정리했어요."))
        self.assertNotIn("지도에서 확인하세요.", out["notebook"]["summary"])

    def test_rules_and_restricted_turns_never_carry_provider_wording(self) -> None:
        profile = Profile(business=self.cafe)
        text = "제주에서 작은 카페를 열고 싶어요"
        local = finalize_turn({"profile": asdict(profile)}, text, profile)
        choices = [dict(choice) for choice in local["notebook"]["choices"]]
        reply = "가게를 열 동네로 제주시와 서귀포시 중 어디가 편하세요?"
        for agent in (
            {"used": False, "steps": []},
            {"used": False, "reason": "제한 문맥 로컬 처리"},
            {"used": True, "path": "local", "reason": "제한 문맥 로컬 처리"},
            {"used": True, "path": "rules", "reason": "모델 호출 실패"},
        ):
            with self.subTest(agent=agent):
                out = finalize_turn(
                    {"profile": asdict(profile), "agent": agent, "agentReply": "말씀대로 정리했어요.",
                     "question": {"text": reply, "options": choices}},
                    text, profile,
                )
                self.assertEqual(out["agentReply"], "")
                self.assertEqual(out["question"]["text"], local["question"]["text"])

    def test_model_question_wording_needs_the_same_choices(self) -> None:
        profile = Profile(business=self.cafe)
        text = "제주에서 작은 카페를 열고 싶어요"
        local = finalize_turn({"profile": asdict(profile)}, text, profile)
        self.assertEqual(local["notebook"]["state"], "question")
        choices = local["notebook"]["choices"]

        same = finalize_turn(
            {"profile": asdict(profile), "agent": {"used": True, "path": "model"},
             "question": {"text": "가게를 열 동네로 어디가 편하세요?", "options": [dict(c) for c in choices]}},
            text, profile,
        )
        self.assertEqual(same["question"]["text"], "가게를 열 동네로 어디가 편하세요?")
        self.assertEqual(same["question"]["options"], choices)
        self.assertIn("가게를 열 동네로 어디가 편하세요?", same["message"])
        self.assertEqual(same["notebook"]["summary"], local["notebook"]["summary"])

        other = finalize_turn(
            {"profile": asdict(profile), "agent": {"used": True, "path": "model"},
             "question": {"text": "바닷가와 시내 중 어디가 좋으세요?", "options": [
                 {"label": "바닷가", "text": "바닷가"}, {"label": "시내", "text": "시내"},
                 {"label": "모름", "text": "모름"}]}},
            text, profile,
        )
        self.assertEqual(other["question"]["text"], local["question"]["text"])
        self.assertEqual(other["question"]["options"], choices)

    def test_model_sentence_cannot_override_a_required_region(self) -> None:
        profile = Profile(business=self.cafe, region="서귀포시", regionResolved=True,
                          decisionPreference="subShare", requiredConditions=["region"])
        text = "서귀포에서만 카페를 하려고 해요"
        out = finalize_turn(
            {"profile": asdict(profile), "agent": {"used": True, "path": "model"},
             "agentReply": "제주시 안에서 같은 업종 비중이 낮은 동네부터 보여 드렸어요."},
            text, profile,
        )
        self.assertEqual(out["agentReply"], "")
        self.assertTrue(all(self.engine.by_code[code]["sgg"] == "서귀포시" for code in out["candidates"]))

    def test_comparison_explains_one_named_dong_against_the_same_pool_without_reranking(self) -> None:
        profile = Profile(business=self.cafe, regionResolved=True, decisionPreference="anchors")
        plain = self.engine.consultation_decision(profile, "")
        decision = self.engine.consultation_decision(profile, "왜 이도2동이고 다른 곳은 왜 아닌가요?")

        # The ranking itself must not move because a comparison was asked for.
        self.assertEqual(decision["candidates"], plain["candidates"])
        self.assertEqual(decision["basis"], "anchors")

        comparison = decision["comparison"]
        self.assertEqual(set(comparison), {"basisLabel", "source", "date", "rows", "summary"})
        self.assertEqual(comparison["basisLabel"], "생활시설 등록점포 수")
        self.assertIn("793", comparison["source"])
        self.assertEqual(comparison["date"], self.engine.meta["storeDate"])
        self.assertEqual(len(comparison["rows"]), 2)
        for row in comparison["rows"]:
            self.assertEqual(set(row), {"id", "name", "value", "detail", "status"})
            self.assertIn(row["status"], ("eligible", "outsideTop3", "excluded", "missing"))
            self.assertIn(row["id"], self.engine.by_code)

        by_name = {row["name"]: row for row in comparison["rows"]}
        self.assertEqual(by_name["이도2동"]["id"], "50110540")
        self.assertEqual(by_name["이도2동"]["value"], self.engine.anchor_count(self.engine.by_code["50110540"]))
        self.assertEqual(by_name["이도2동"]["status"], "eligible")
        # A single named dong is compared against the engine's own first candidate.
        self.assertEqual(by_name[plain["candidateNames"][0]]["status"], "eligible")
        # Actual 793 counts and 830 activity appear as supporting evidence, never as a fake zero.
        self.assertIn("793 상가 기준", by_name["이도2동"]["detail"])
        self.assertIn("830 사업체 활동", by_name["이도2동"]["detail"])
        # Both rates are labelled: the dong's own rate and 제주 전체, never two bare numbers.
        island = self.engine.island_growth
        self.assertIn(f"증감률은 이도2동 +8.0%, 제주 전체 {island:+.1f}%입니다", by_name["이도2동"]["detail"])
        self.assertIn(f"제주 전체 {island:+.1f}%", by_name["노형동"]["detail"])
        self.assertIn(f"전체 {len(self.engine.dongs)}곳", comparison["summary"])
        self.assertIn("근거 값이 확인되지 않은 후보는 0곳", comparison["summary"])
        self.assertIn("더 낫다는 뜻이 아닙니다", comparison["summary"])
        self.assertNotIn("더 우수", comparison["summary"])
        # The comparison text rides on the same local summary, it does not replace it.
        self.assertTrue(decision["summary"].startswith(plain["summary"]) or comparison["summary"] in decision["summary"])

    def test_two_named_dongs_compare_directly_without_changing_preference_or_ranking(self) -> None:
        profile = Profile(business=self.cafe, regionResolved=True, decisionPreference="subShare")
        decision = self.engine.consultation_decision(profile, "노형동과 이도2동 비교해 주세요.")

        self.assertEqual(decision["basis"], "subShare")
        self.assertEqual(decision["profile"].decisionPreference, "subShare")
        self.assertEqual([row["name"] for row in decision["comparison"]["rows"]], ["노형동", "이도2동"])
        for row in decision["comparison"]["rows"]:
            dong = self.engine.by_code[row["id"]]
            self.assertEqual(row["value"], dong["sub"][str(self.cafe)] / dong["total"])
        # Neither named dong is in the displayed top three on this basis, and the rows say so honestly.
        self.assertTrue(all(row["status"] == "outsideTop3" for row in decision["comparison"]["rows"]))
        self.assertIn("상위 3곳 밖", decision["comparison"]["summary"])
        self.assertEqual(decision["candidates"], [
            row["id"] for row in self.engine.rank_consultation(decision["profile"], "subShare")
        ])

    def test_comparison_separates_excluded_dongs_from_eligible_ones(self) -> None:
        profile = Profile(business=self.cafe, region="서귀포시", regionResolved=True,
                          decisionPreference="anchors", requiredConditions=["region"])
        decision = self.engine.consultation_decision(profile, "이도2동과 노형동은 왜 후보가 아닌가요?")

        comparison = decision["comparison"]
        self.assertEqual([row["status"] for row in comparison["rows"]], ["excluded", "excluded"])
        for row in comparison["rows"]:
            self.assertIsNone(row["value"])
            self.assertIn("서귀포시가 아님", row["detail"])
            # Numbers stay available even when a required condition removes the dong.
            self.assertIn("793 상가 기준", row["detail"])
        self.assertIn("필수 조건에서 26곳이 빠져", comparison["summary"])
        self.assertIn("서귀포시가 아님 26곳", comparison["summary"])
        pool_size = len(self.engine.rank_consultation(profile, "anchors", limit=None))
        self.assertIn(f"같은 후보 풀 {pool_size}곳", comparison["summary"])
        self.assertTrue(all(self.engine.by_code[code]["sgg"] == "서귀포시" for code in decision["candidates"]))

    def test_comparison_reports_missing_evidence_as_missing_not_zero(self) -> None:
        engine = Engine()
        engine.anchor_subs = [1]
        engine.dongs = [
            {"code": "00000001", "name": "표본동1", "sgg": "제주시", "total": 10, "sub": {"0": 4, "1": 2},
             "bizStats": {"active": {"2018": 10, "2023": 12}}},
            {"code": "00000002", "name": "표본동2", "sgg": "제주시", "total": 0, "sub": {"0": 0, "1": 0},
             "bizStats": {"active": {"2018": 5, "2023": 4}}},
            {"code": "00000003", "name": "표본동3", "sgg": "제주시", "total": None, "sub": {"0": 1}},
        ]
        engine.by_code = {dong["code"]: dong for dong in engine.dongs}
        profile = Profile(business=0, regionResolved=True, decisionPreference="subShare")

        decision = engine.consultation_decision(profile, "표본동2와 표본동3 비교해 주세요")
        rows = {row["name"]: row for row in decision["comparison"]["rows"]}
        self.assertEqual(set(rows), {"표본동2", "표본동3"})
        self.assertEqual(rows["표본동2"]["status"], "missing")
        self.assertEqual(rows["표본동3"]["status"], "missing")
        # 0/0 and 1/None are not shares, so the row value stays null instead of becoming 0.
        self.assertIsNone(rows["표본동2"]["value"])
        self.assertIsNone(rows["표본동3"]["value"])
        self.assertIn("전체 등록점포가 0곳", rows["표본동2"]["detail"])
        self.assertIn("분모가 확인되지 않아", rows["표본동3"]["detail"])
        # 830 activity is present for 표본동2, so it is reported, and missing for 표본동3.
        self.assertIn("830 사업체 활동은 2018년 5곳에서 2023년 4곳으로 -1곳", rows["표본동2"]["detail"])
        self.assertNotIn("830 사업체 활동", rows["표본동3"]["detail"])
        self.assertIn("근거 값이 확인되지 않은 후보는 2곳", decision["comparison"]["summary"])
        self.assertNotIn("0.0%", decision["comparison"]["summary"])

    def test_comparison_is_absent_unless_a_dong_is_named_and_asked_about(self) -> None:
        profile = Profile(business=self.cafe, regionResolved=True, decisionPreference="anchors")
        for text in ("카페를 열 때 생활시설 주변을 더 중요하게 봐요.", "연동 보여 주세요"):
            with self.subTest(text=text):
                decision = self.engine.consultation_decision(profile, text)
                self.assertNotIn("comparison", decision)

    def test_finalized_turn_carries_the_same_comparison_under_the_notebook(self) -> None:
        profile = Profile(business=self.cafe, regionResolved=True, decisionPreference="anchors")
        text = "왜 이도2동이고 다른 곳은 왜 아닌가요?"
        local = self.engine.consultation_decision(profile, text)
        out = finalize_turn(
            {"profile": asdict(profile), "agent": {"used": True, "path": "model"},
             "agentReply": "말씀하신 기준으로 후보를 정리했어요."},
            text, profile,
        )
        self.assertEqual(local["candidates"], out["candidates"])
        self.assertEqual(out["notebook"]["comparison"], local["comparison"])
        self.assertNotIn("comparison", out)  # exposed once, under the notebook

        plain = finalize_turn({"profile": asdict(profile)}, "", profile)
        self.assertNotIn("comparison", plain["notebook"])


if __name__ == "__main__":
    unittest.main()
