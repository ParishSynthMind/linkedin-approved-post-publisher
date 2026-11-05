from li_publisher.brief import BriefError, parse_brief
from li_publisher.draft import Draft
from li_publisher.validate import validate

from .conftest import GOOD_TEXT


def codes(report):
    return {f.code for f in report.errors}


def test_good_draft_passes(brief):
    r = validate(Draft(GOOD_TEXT, ["Automation", "SmallBusiness"], [0, 1, 2]), brief)
    assert r.ok, r.as_text()


def test_invented_statistic_is_blocked(brief):
    r = validate(Draft(GOOD_TEXT + " It cut errors by 87%.", ["Automation"]), brief)
    assert "unverified_figure" in codes(r)


def test_research_claim_is_blocked(brief):
    r = validate(Draft(GOOD_TEXT + " Studies show teams love it.", []), brief)
    assert "unsourced_research" in codes(r)


def test_em_and_en_dashes_blocked(brief):
    assert "dash" in codes(validate(Draft(GOOD_TEXT.replace(",", " —", 1), []), brief))
    assert "dash" in codes(validate(Draft(GOOD_TEXT.replace(",", " –", 1), []), brief))


def test_hashtag_spam_and_format(brief):
    r = validate(Draft(GOOD_TEXT, ["AI", "Automation", "Ops", "Finance"]), brief)
    assert "hashtag_spam" in codes(r)
    r = validate(Draft(GOOD_TEXT, ["bad tag", "AI", "ai"]), brief)
    assert {"hashtag_format", "hashtag_duplicate"} <= codes(r)
    r = validate(Draft(GOOD_TEXT + " #automation", []), brief)
    assert "hashtag_in_body" in codes(r)


def test_length_limits(brief):
    assert "too_short" in codes(validate(Draft("Short post?", []), brief))
    long = (GOOD_TEXT + " ") * 10
    assert "too_long" in codes(validate(Draft(long, []), brief))


def test_banned_claims_links_mentions(brief):
    r = validate(Draft(GOOD_TEXT + " Zero errors, guaranteed. See https://example.com @someone", []), brief)
    assert {"banned_claim", "unapproved_link", "mention"} <= codes(r)


def test_allowed_link_passes():
    b = parse_brief("topic: t\nallowed_links: https://example.com/case\n")
    text = "A short write up of the project and what I learned along the way. https://example.com/case"
    assert validate(Draft(text, []), b).ok


def test_unknown_fact_index(brief):
    assert "unknown_fact" in codes(validate(Draft(GOOD_TEXT, [], [7]), brief))


def test_warnings_do_not_block(brief):
    r = validate(Draft(GOOD_TEXT + " This is a game-changer. Comment YES below.", []), brief)
    assert r.ok
    assert {w.code for w in r.warnings} >= {"ai_phrasing", "engagement_bait"}


def test_brief_parsing_and_errors(brief):
    assert brief.max_chars == 1100 and len(brief.facts) == 3 and brief.banned_claims == ["zero errors"]
    assert brief.notes == "Keep it short."
    for bad in ("audience: x\n", "topic: t\nmax_chars: lots\n", "topic: t\nmax_chars: 9000\n",
                "topic: t\nnonsense line\n", "topic: t\nmax_hashtags: 9\n"):
        try:
            parse_brief(bad)
        except BriefError:
            continue
        raise AssertionError(f"accepted bad brief {bad!r}")


def test_content_hash_ignores_spacing_and_case():
    a = Draft("Hello   world post", ["AI"])
    b = Draft("hello world\npost", ["ai"])
    assert a.content_hash() == b.content_hash()
    assert a.commentary() == "Hello   world post\n\n#AI"
