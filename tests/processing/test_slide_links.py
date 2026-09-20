"""The sites a carousel names, pulled out of a transcription.

These run on model output, which is the whole reason the rules are narrow: a domain here
is a vision model's reading of some letters on a slide, and one wrong character is a
different company. So the assertions are mostly about what is *refused*.
"""
from __future__ import annotations

from app.services import slide_links

SLIDE = """GERMANY (PART 1)
1. Make it in Germany — Official Government
2. Bundesagentur für Arbeit — Government
3. EURES Germany — EU Official
4. relocate.me — Visa Sponsorship Jobs
5. arbeitnow — Visa Jobs
9. Jobbörse.de — General Jobs
10. joblift — Job Aggregator
Image: a drawing of the Brandenburg Gate.
"""


def test_the_sites_a_slide_names_come_out_in_slide_order() -> None:
    """Including the ones with non-ASCII letters, which an ASCII pattern drops in silence."""
    assert slide_links.extract(SLIDE) == ["https://relocate.me", "https://jobbörse.de"]


def test_a_site_named_twice_is_one_entry_at_its_first_position() -> None:
    text = "1. relocate.me\n2. stepstone.de\n7. Relocate.ME again"
    assert slide_links.extract(text) == ["https://relocate.me", "https://stepstone.de"]


def test_the_scheme_is_imposed_not_parsed() -> None:
    """Nothing from the text decides where a tap goes but the host itself."""
    for hostile in (
        "javascript:alert(1)//evil.com",
        "http://user:pass@evil.com",
        "https://good.com/../../etc/passwd",
    ):
        for url in slide_links.extract(hostile):
            assert url.startswith("https://")
            assert "@" not in url and "/" not in url.removeprefix("https://")


def test_prose_that_looks_like_a_domain_is_refused() -> None:
    """A transcription is sentences, and a sentence is full of dots."""
    prose = "Save this post. It is worth.it and check.in later, e.g. tomorrow. No.1 tip."
    # `.it` and `.in` are real ccTLDs and deliberately absent from `_TLDS`: they collide
    # with English words far more often than they name a site. See that constant.
    assert slide_links.extract(prose) == []


def test_ip_literals_are_never_a_site_on_a_slide() -> None:
    assert slide_links.extract("visit 169.254.169.254 now") == []


def test_the_list_is_capped() -> None:
    many = "\n".join(f"{i}. site{i}x.com" for i in range(200))
    assert len(slide_links.extract(many)) == slide_links.MAX_LINKS
