from legal_agent.policy import load_policy, red_line_hits, rules_for


def test_policy_loads_with_unique_ids_and_known_doc_types():
    rules = load_policy()
    ids = [rule.id for rule in rules]

    assert len(ids) == len(set(ids))
    assert all(set(rule.applies_to) <= {"nda", "msa", "dpa", "sow"} for rule in rules)
    assert all(rule.accept or rule.push_back or rule.escalate for rule in rules)


def test_rules_for_doc_type():
    ids = {rule.id for rule in rules_for(load_policy(), "dpa")}
    assert {"subprocessors", "breach-notification", "governing-law"} <= ids
    assert "nda-term" not in ids


def test_red_lines_catch_sanctioned_countries_and_uncapped_liability():
    rules = load_policy()
    assert red_line_hits(rules, "governed by the laws of the Russian Federation") == ["governing-law"]
    assert red_line_hits(rules, "Provider's liability is uncapped for data breaches") == ["liability-cap"]


def test_red_lines_ignore_normal_text():
    assert red_line_hits(load_policy(), "governed by the laws of England and Wales") == []
