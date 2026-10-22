from __future__ import annotations

import copy
import hashlib

import pytest

from evals.batches import catalog, source_catalog
from evals.common import canonical, digest, read
from evals.holdout import (
    CASE_IDS,
    FAMILIES,
    REGISTRY_PATH,
    REGISTRY_SHA256,
    VERSION,
    create_release_set,
    holdout_cases,
    validate_holdout,
    validate_release_set,
)

OLD_V1_DATA_SHA256 = "cb5e2c6a9f54031095f25f8a4a41314b37a863a53f3a2795a1b0a3d4163c12fc"
OLD_V1_REGISTRY_SHA256 = "244d87c83fa5d55dd7f5bf2441a04df01c3f268ef6a603afc83ea21d904630fc"
OLD_V1_INPUT_SHA256 = {
    "b2df4bcfa35ebecdfd48aedfc14095fec9407c140d8ccb00576fe33ddea25834",
    "75b2e4cc181efa828c247fda227f6d4d7f665b5f07affd34106ade72b787082b",
    "e508bfd045f6959773cc7933ee85b53cf124da8ee52b928238645e03816f8113",
    "255143cbdef384e54ec6d0448e1f08e70e99af3ad09ceb70070120fde08fc423",
    "ceef07ed76a1ad476f276750f57030495e2b2c0cebaf23f386611e9115f2eabd",
    "0a4c6a4c3400059bd49256cf54fd7b0cfe7c917a53efed6634aefca5c3103a2e",
    "548681d67e7ac99cd36bac67dc4d2dcecaed8d527f2ab25ecd66de5ae36dc558",
    "81b6475bcba6cce9126bd0ab18ba6a3b16200bdab0d45d8b6108ff5e647ed78d",
    "8ba93a327ea976bf47dbdb12952ee4b7a88264423a971b73d73937732974cda0",
    "67f1d088f58ade6cb3a0ca57e6c58cf529e0fbef0bce76cd578ef73d7428714e",
    "6268d070f1dcbbbdcc636a17c69e1d7d4e9288ef72a33c1b12719f424ba79a5a",
    "a21930c392d7a74c8d9bfe609c752d305e1ba9e063792d54ebee52c6e5635e2a",
    "1cc2da3511d9dbb1b5b80084cb4300c483ebaa5f494402f20b30c0876f95bb6a",
    "6b0cefbca32b2cafe9523e56e1f97942f85eb8369efddfd2f85bac448d905c53",
    "d1bd3fd39d72242e4327a531cd9484451e30d1e9b67fdd443049e026652cce24",
    "ccf6e6a08b48388f5c67d5e031623a328782eefd2faf668de67c63f89ff1c27b",
    "cf7e53cbfeb58a65efeaa9dcd794852e17f309af85fa593780f0cb1f2ad436de",
    "b9c71ca053a3a319c95660f359f34f164ff13fc91c35922d54f17b292cfe1d4c",
    "b50a3fe67edfdb57c01f1a01d28ebad7886c846ff47fdb2471a2b17d9faa9a10",
    "4794f1ba87fc6c6658f556e17aee324ad38fd1c1a3c2ec3383abfa8fc762e076",
}
OLD_V1_NON_BOUNDARY_GOLD_SHA256 = {
    "a0690000b500c1dccc00dd1165e8d6950127f0267fcc104bbb0f9643190fc0eb",
    "643dcf4f9a435bb857f70e21a8e7f151c61909b92eaba41bffc4a51aa663ac56",
    "4547cb6bc1ed91e9c25d10a5249ed2d5fc53ad6e3bb026dec217ef95170d1d29",
    "5bb0816bcd2add13a064dcc2d8606ab0bce2431c09a961ab7ddbde6a85996822",
    "6d788f7232194453aaec262fc64bd2d546e37b4e5ff6911a3238ccbf4240944f",
    "ea8422157ef3fb99cbaf97cde2f95eb950b8988c7c7618b0a41002b668dbc875",
    "c943271a0fa706f252c5e93599d36e018427853c89c258f9ab8b0bd23525bedb",
    "694c00232911657b2e8ba94131cdb96bd9ab7dc684957140635df677314f8460",
    "a90d4fa721b85806d08509620e9cfabb7ecdd71ceee108b6b67391bf17e74197",
    "26f9aa5d5e4bf153393a62e73482b1a4e302e695b0d9203546e0b74c14c68a28",
    "d005bcc9fe7be92cf91d3da2b82724530483100e6cf63fffe352586d189c3a1e",
    "1e983461993a317c155dd0579cd42ea4e33a9ab553d047496cacb8bae684a353",
    "12b4238fda9b6f7b031c7a4ec139e27d46906f93ec32eea6174b992786517c8b",
    "3c4eb8e284099cd8e5e8659dbb5c86d68ad368c32ea3bc67f83f216100f26813",
    "5bab6ff27d8afeb20ec1252558af0a94ee313861eb2116406bb6740d66f47bf1",
}
OLD_V1_1_DATA_SHA256 = "952bdcca57dd820745aa6b3352289ad990f8d9ad7d618ecac17945dab06c1971"
OLD_V1_1_REGISTRY_SHA256 = "48bbcca78b1124792a43e4b62cedd85c6ae9dc1d6d9f0ec63b1635668249c653"
OLD_V1_1_INPUT_SHA256 = {
    "9527221b6319f44062c00be249a9b7997ccd446c98950ef1b785d6a77062e698",
    "fba138da8394ca41285b32405bd02408b4dd842c3e331acc86a211a4f7e7ac5b",
    "76a20cf0476372b6deb4e3b750aac915b799be28a92b216eca0f123d39b851d8",
    "4b195e0c53fdf0a58fb7c9a47d9e44f72ae11081f5adacda0c4befa92dbe2ff5",
    "fb92fff2607634ac1dffa10ea7942cb4f2b3d95ace1b7818f6737e1f5b0aee98",
    "26a225439742c8003760dc4ac874fad58105e1e3ddb46564ba42935f6a9439cc",
    "66b78a97efb023f24b02ed2b05e47806cf3be298734113f185200304f2f86146",
    "8972a20b6c3a575ef8bb0344639187a3f8ca48926e549a51c0a1f236694b11ed",
    "2162a859f0d9948a47a9613f98a765c9ff521ab33a7be2dcea704cc8896e7116",
    "5ece48c21a18b6b6b1dc702168c3249bd03822af5d0c7a52b767a61609063a08",
    "5ded88667a67c02ffdc9ee28ac44fe3e9a638a7ed67149f8a99a7434f3c107c5",
    "ab6a587c0fbea67143780f27e7c6fd3267a8b3d794a066cc6f3b3938f99d776c",
    "a8f2703d6ca9b449f4acf410ba86ec93c0ba61c3134dd694b7f572fd4f66fd05",
    "a0b09135c07f6d02869d53daf7a1a0dcaec995ada55c44e0b6d28b5ca79a219d",
    "8fd47fe6a993af4e9084ae270dac24efd28ce214dbfd0a5d1e636ebd1fadef68",
    "0e6919b9bc8c8e0ab970973917be8b90b49ac26e4047e7edb43c4a0efba7f055",
    "60f81826eb4f04e3587bd7b73e47d78c86c03c8bd24597ff274854f0eec26a90",
    "9381621b35305c3e64c9b0b14575327322885c1c10451501cddb690df9a85fd9",
    "7bd1a170177e49333dec6984289f093d30f8d812c30faa724a5c91ba979c3f91",
    "1e4657e82b0a56face2db1a73e57f2e3c644452636d4b4cba4f2b0d36c3d6d90",
}


def test_holdout_inventory_is_frozen_and_program_scored():
    cases = holdout_cases()
    assert tuple(case["case_id"] for case in cases) == CASE_IDS
    assert all(case["case_version"] == VERSION == "phase8-holdout/1.2" for case in cases)
    assert all(case["split"] == "holdout" and case["scope"] == "full_task" for case in cases)
    assert all(case["scoring"]["method"] == "program" for case in cases)
    assert all(case["scoring"]["strict_json_types"] is True for case in cases)
    assert all(case["scoring"]["formal_quality_eligible"] is False for case in cases)
    assert all(case["initial_state"]["fixtures"] == [] for case in cases)
    assert digest(REGISTRY_PATH.read_bytes()) == REGISTRY_SHA256
    assert REGISTRY_SHA256 != OLD_V1_REGISTRY_SHA256
    assert REGISTRY_SHA256 != OLD_V1_1_REGISTRY_SHA256
    assert read(REGISTRY_PATH)["data_sha256"] != OLD_V1_DATA_SHA256
    assert read(REGISTRY_PATH)["data_sha256"] != OLD_V1_1_DATA_SHA256
    new_inputs = {hashlib.sha256(case["turns"][0]["input"].encode()).hexdigest()
                  for case in cases}
    assert len(new_inputs) == 20
    assert new_inputs.isdisjoint(OLD_V1_INPUT_SHA256)
    assert new_inputs.isdisjoint(OLD_V1_1_INPUT_SHA256)
    new_non_boundary_golds = {
        hashlib.sha256(canonical(case["requirements"][0]["expected"])).hexdigest()
        for case in cases if "strict_boundary" not in case["tags"]
    }
    assert new_non_boundary_golds.isdisjoint(OLD_V1_NON_BOUNDARY_GOLD_SHA256)
    assert [case["requirements"][0]["expected"]["eligible"] for case in cases[15:]] == [
        True, False, False, False, True]

    report = validate_holdout(catalog(), cases)
    assert report["status"] == "pass"
    assert report["holdout_count"] == 20
    assert report["families"] == {family: 5 for family in FAMILIES}
    assert report["public_case_count"] == 120
    assert report["public_input_clause_count"] >= 120
    assert report["id_overlap_count"] == 0
    assert report["literal_input_or_fixture_overlap_count"] == 0
    assert report["content_hash_overlap_count"] == 0
    assert report["all_gold_recomputed"] is True
    assert report["release_profile_only"] is True
    assert report["development_and_nightly_access"] == "forbidden"

    source_report = validate_holdout(source_catalog(), cases)
    assert source_report["public_fixture_count"] > 0
    assert source_report["public_input_clause_count"] >= 120


def test_public_fixture_overlap_is_rejected():
    cases = holdout_cases()
    public = catalog()
    public["V001"]["fixtures"]["copied-input.txt"] = cases[0]["turns"][0]["input"]
    with pytest.raises(ValueError, match="holdout_public_text_overlap"):
        validate_holdout(public, cases)


def test_changed_gold_and_family_identity_are_rejected():
    cases = holdout_cases()
    wrong_gold = copy.deepcopy(cases)
    wrong_gold[5]["requirements"][0]["expected"]["quantity"] = "7"
    with pytest.raises(ValueError, match="holdout_case_gold_changed"):
        validate_holdout(catalog(), wrong_gold)

    changed_family = copy.deepcopy(cases)
    changed_family[10]["tags"][-1] = "arithmetic"
    with pytest.raises(ValueError, match="holdout_family_tampered"):
        validate_holdout(catalog(), changed_family)


def test_fixed_registry_source_hash_rejects_case_hash_changes():
    cases = holdout_cases()
    assert all(case["source_sha256"] == REGISTRY_SHA256 for case in cases)
    changed_source = copy.deepcopy(cases)
    changed_source[0]["source_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="holdout_identity_or_provenance"):
        validate_holdout(catalog(), changed_source)


def test_wrong_registry_gold_is_rejected_by_independent_formula():
    # A typed string cannot pass as a numeric gold value even if JSON values
    # would otherwise compare loosely in a legacy scorer.
    cases = holdout_cases()
    wrong_gold = copy.deepcopy(cases)
    wrong_gold[0]["requirements"][0]["expected"]["total"] = 281.0
    with pytest.raises(ValueError, match="holdout_case_gold_changed"):
        validate_holdout(catalog(), wrong_gold)


def test_release_sets_are_fresh_across_nonces_and_ignore_reported_validation():
    first_nonce = "11111111-1111-4111-8111-111111111111"
    second_nonce = "22222222-2222-4222-8222-222222222222"
    first = create_release_set(first_nonce, catalog())
    second = create_release_set(second_nonce, catalog())

    assert len(first["cases"]) == len(second["cases"]) == 20
    assert len({case["turns"][0]["input"] for case in first["cases"]}) == 20
    first_inputs = {case["turns"][0]["input"] for case in first["cases"]}
    second_inputs = {case["turns"][0]["input"] for case in second["cases"]}
    assert first_inputs.isdisjoint(second_inputs)
    static_inputs = {case["turns"][0]["input"] for case in holdout_cases()}
    assert first_inputs.isdisjoint(static_inputs)
    assert first["data_sha256"] != second["data_sha256"]
    assert first["validation"]["all_gold_recomputed"] is True
    assert first["validation"]["static_input_overlap_count"] == 0
    assert first["validation"]["public_text_or_fixture_overlap_count"] == 0
    assert all(case["source_sha256"] == first["data_sha256"] for case in first["cases"])
    assert all(case["source_ref"] == f"generated-release-set.json#{case['case_id']}"
               for case in first["cases"])

    first["validation"] = {"status": "tampered"}
    report = validate_release_set(first, catalog())
    assert report["status"] == "pass"
    assert report["nonce"] == first_nonce


def test_release_set_gold_is_recomputed_from_facts():
    bundle = create_release_set("33333333-3333-4333-8333-333333333333", catalog())
    tampered = copy.deepcopy(bundle)
    tampered["data"]["cases"][0]["expected"]["total"] += 1
    tampered["data_sha256"] = digest(canonical(tampered["data"]))
    with pytest.raises(ValueError, match="release_gold_recompute_mismatch"):
        validate_release_set(tampered, catalog())


def test_release_set_rejects_invalid_nonce_and_public_overlap():
    with pytest.raises(ValueError, match="invalid_release_nonce"):
        create_release_set("not-a-uuid", catalog())

    bundle = create_release_set("44444444-4444-4444-8444-444444444444", catalog())
    public = catalog()
    public["V001"]["fixtures"]["copied-input.txt"] = bundle["data"]["cases"][0]["input"]
    with pytest.raises(ValueError, match="release_public_text_overlap"):
        validate_release_set(bundle, public)
