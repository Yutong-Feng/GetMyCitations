import json
import os
import signal
import sys
from datetime import datetime, timezone

from scholarly import scholarly, ProxyGenerator

GOOGLE_SCHOLAR_ID = os.environ["GOOGLE_SCHOLAR_ID"]
RESULTS_DIR = "results"
RESULTS_FILE = os.path.join(RESULTS_DIR, "gs_data.json")

# Overall wall-clock budget, well under the GitHub Actions job's
# timeout-minutes. If anything gets stuck despite the bounds below, this
# forces a clean, informative failure instead of a hard SIGKILL.
HARD_DEADLINE_SECONDS = 12 * 60

# Bound every request so a blocked/CAPTCHA'd request fails fast instead of
# hanging for hours, which is why past runs kept getting killed after
# GitHub Actions' 6-hour job limit.
scholarly.set_timeout(30)
scholarly.set_retries(3)


class DeadlineExceeded(Exception):
    pass


def _on_alarm(signum, frame):
    raise DeadlineExceeded(f"Exceeded {HARD_DEADLINE_SECONDS}s hard deadline")


def setup_proxy() -> None:
    """Route requests through ScraperAPI if a key is configured.

    Google Scholar frequently CAPTCHA-blocks GitHub Actions' shared IP
    ranges outright, which is the main reason this job used to hang.
    ScraperAPI (free tier: 1000 requests/month, set as the SCRAPER_API_KEY
    secret) works around that reliably. Free rotating proxies were tried
    here too, but in practice they are slow and unreliable enough to make
    things worse, so without a key we just go direct — scholarly's own
    timeout/retry bounds above keep that path from hanging.
    """
    scraper_api_key = os.environ.get("SCRAPER_API_KEY")
    if not scraper_api_key:
        print("No SCRAPER_API_KEY set; continuing without a proxy.")
        return
    try:
        pg = ProxyGenerator()
        if pg.ScraperAPI(scraper_api_key):
            scholarly.use_proxy(pg)
            print("Using ScraperAPI proxy.")
            return
    except Exception as exc:
        print(f"Proxy setup failed ({exc}); continuing without a proxy.")
        return
    print("Proxy setup did not succeed; continuing without a proxy.")


def load_previous_bibs() -> dict:
    """Return {author_pub_id: bib} from the previous run, if any, so we
    don't re-fetch per-publication author lists that can't change."""
    if not os.path.exists(RESULTS_FILE):
        return {}
    try:
        with open(RESULTS_FILE) as f:
            previous = json.load(f)
        return {
            pub_id: pub["bib"]
            for pub_id, pub in previous.get("publications", {}).items()
            if pub.get("bib", {}).get("author")
        }
    except (json.JSONDecodeError, KeyError):
        return {}


def _name_key(name: str) -> tuple:
    # Google Scholar sometimes abbreviates given names ("Y Feng" vs
    # "Yutong Feng"), so compare on last name + first-name initial.
    parts = name.replace(".", "").split()
    return (parts[-1].lower(), parts[0][0].lower()) if parts else ("", "")


def is_first_author(bib_author: str, my_name: str) -> bool:
    first_author = bib_author.split(" and ")[0].strip()
    return _name_key(first_author) == _name_key(my_name)


def main() -> None:
    setup_proxy()

    author = scholarly.search_author_id(GOOGLE_SCHOLAR_ID)
    scholarly.fill(author, sections=["basics", "indices", "counts", "publications"])

    previous_bibs = load_previous_bibs()
    publications = {}
    first_author_count = 0

    for pub in author["publications"]:
        pub_id = pub["author_pub_id"]
        cached_bib = previous_bibs.get(pub_id)
        if cached_bib:
            pub["bib"].update(cached_bib)
        else:
            try:
                scholarly.fill(pub)
            except Exception as exc:
                print(f"Could not fetch full details for {pub_id}: {exc}")

        if pub["bib"].get("author") and is_first_author(pub["bib"]["author"], author["name"]):
            first_author_count += 1

        publications[pub_id] = pub

    author["publications"] = publications
    author["updated"] = datetime.now(timezone.utc).isoformat()
    author["first_author_count"] = first_author_count

    total_papers = len(publications)
    citations = author.get("citedby", 0)
    h_index = author.get("hindex", 0)

    print(f"Name: {author['name']}")
    print(f"Total papers: {total_papers}")
    print(f"First-author papers: {first_author_count}")
    print(f"Citations: {citations}")
    print(f"h-index: {h_index}")

    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(RESULTS_FILE, "w") as outfile:
        json.dump(author, outfile, ensure_ascii=False)

    badges = {
        "gs_data_shieldsio.json": ("citations", citations),
        "gs_hindex_shieldsio.json": ("h-index", h_index),
        "gs_total_papers_shieldsio.json": ("papers", total_papers),
        "gs_first_author_shieldsio.json": ("1st-author papers", first_author_count),
    }
    for filename, (label, message) in badges.items():
        shieldio_data = {
            "schemaVersion": 1,
            "label": label,
            "message": str(message),
        }
        with open(os.path.join(RESULTS_DIR, filename), "w") as outfile:
            json.dump(shieldio_data, outfile, ensure_ascii=False)


if __name__ == "__main__":
    signal.signal(signal.SIGALRM, _on_alarm)
    signal.alarm(HARD_DEADLINE_SECONDS)
    try:
        main()
    except Exception as exc:
        print(f"Failed to fetch Google Scholar data: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        signal.alarm(0)
