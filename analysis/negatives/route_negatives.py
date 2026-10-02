"""Find search terms in a source campaign that belong to a destination campaign,
confirm the destination can serve them, and propose phrase-match negatives for
the source campaign that block only those terms.

Google Ads negative-match rules used here (negatives do NOT expand to close variants):
  EXACT  negative  -> query tokens == negative tokens
  PHRASE negative  -> negative tokens appear contiguously, in order, in the query
  BROAD  negative  -> every negative token appears somewhere in the query

Inputs are raw JSON exports from the Google Ads search tool ({"result": [...]}).
"""
import argparse
import json
import re
from collections import defaultdict

# A term mentioning any of these stays in the Protein campaign, whatever else it says.
PROTEIN_TOKENS = {"protein", "proteins", "whey", "casein", "isolate", "bcaa", "creatine", "collagen"}
# A term needs one of these to count as weight-loss intent.
WL_PATTERNS = [r"\bweight loss\b", r"\blose weight\b", r"\blosing weight\b", r"\bslimming\b", r"\bdiet\b",
               r"\bdiets\b", r"\bfat loss\b", r"\bfat burn", r"\bglp ?1\b", r"\bfasting\b", r"\bappetite\b",
               r"\bkcal\b", r"\blow cal(orie)?\b"]
# Mentions of other goals that make a term not weight-loss.
EXCLUDE_PATTERNS = [r"\bgain\b", r"\bbulk", r"\bmass\b", r"\bmuscle\b"]


FILLER = {"uk", "best", "the", "for", "a", "of", "to", "in", "and", "with", "what", "is", "are", "top", "good"}


def singular(w):
    return w[:-1] if w.endswith("s") and len(w) > 3 else w


def toks(s):
    return re.findall(r"[a-z0-9]+", s.lower())


def contains_seq(hay, needle):
    n = len(needle)
    return any(hay[i:i + n] == needle for i in range(len(hay) - n + 1))


def neg_blocks(term_toks, neg_text, match_type):
    nt = toks(neg_text)
    if match_type == "EXACT":
        return term_toks == nt
    if match_type == "PHRASE":
        return contains_seq(term_toks, nt)
    return set(nt) <= set(term_toks)  # BROAD


def is_protein(tt):
    return bool(PROTEIN_TOKENS & set(tt))


def is_wl(term):
    return any(re.search(p, term) for p in WL_PATTERNS) and not any(re.search(p, term) for p in EXCLUDE_PATTERNS)


def load(path):
    return json.load(open(path))["result"]


def aggregate_terms(rows):
    agg = defaultdict(lambda: defaultdict(float))
    for r in rows:
        t = r["search_term_view.search_term"].lower().strip()
        for m in ("cost_micros", "clicks", "conversions", "conversions_value"):
            agg[t][m] += r.get("metrics." + m) or 0
    return agg


def coverage(term, dest_kws, dest_neg_campaign):
    """Best destination keyword that can serve `term`, and whether a negative blocks it."""
    tt = toks(term)
    for neg, mt in dest_neg_campaign:
        if neg_blocks(tt, neg, mt):
            return None, f"blocked by destination campaign negative [{mt.lower()}] '{neg}'"
    best = None
    for k in dest_kws:
        kt = toks(k["text"])
        if k["match"] == "EXACT" and kt == tt:
            level = (0, "exact keyword")
        elif k["match"] in ("BROAD", "PHRASE") and contains_seq(tt, kt):
            level = (1, f"{k['match'].lower()} keyword contained in term")
        elif k["match"] == "BROAD" and set(kt) <= set(tt):
            level = (2, "broad keyword, all words present")
        elif k["match"] == "BROAD" and set(kt) - FILLER <= {singular(w) for w in tt} | set(tt):
            level = (3, "broad keyword, near match (ignores uk/best/plurals) - likely, not certain")
        else:
            continue
        blocked = [n for n in k["ag_negs"] if neg_blocks(tt, n[0], n[1])]
        if blocked:
            continue
        if best is None or level[0] < best[0][0]:
            best = (level, k)
    if best:
        (_, why), k = best
        return k, why
    return None, "no keyword match; only reachable via broad-match semantics (unconfirmed)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-terms", required=True, help="search_term_view export for the source (Protein) campaign")
    ap.add_argument("--source-keywords", required=True, help="keyword_view export for the source campaign")
    ap.add_argument("--dest-keywords", required=True, help="ad_group_criterion export incl. negatives")
    ap.add_argument("--dest-campaign", default="UK_SEARCH_WEIGHT_LOSS")
    ap.add_argument("--dest-campaign-negatives", required=True, help='JSON list of [text, match_type]')
    ap.add_argument("--min-spend", type=float, default=5.0)
    ap.add_argument("--validate", help="JSON list of [negative, match_type] to test instead of auto-suggesting")
    args = ap.parse_args()

    terms = aggregate_terms(load(args.source_terms))
    src_kw = {r["ad_group_criterion.keyword.text"].lower() for r in load(args.source_keywords)}
    dest_neg_campaign = [tuple(x) for x in json.load(open(args.dest_campaign_negatives))]

    # Live destination keywords, each with its ad group's negatives.
    ag_negs = defaultdict(list)
    kws = []
    for r in load(args.dest_keywords):
        if r["campaign.name"] != args.dest_campaign or r["ad_group.status"] != "ENABLED":
            continue
        if r["ad_group_criterion.status"] == "REMOVED":
            continue
        entry = (r["ad_group_criterion.keyword.text"], r["ad_group_criterion.keyword.match_type"])
        if r["ad_group_criterion.negative"]:
            ag_negs[r["ad_group.name"]].append(entry)
        elif r["ad_group_criterion.status"] == "ENABLED":
            kws.append({"ag": r["ad_group.name"], "text": entry[0], "match": entry[1]})
    for k in kws:
        k["ag_negs"] = ag_negs[k["ag"]]

    # 1. Tag: weight-loss intent with no protein mention.
    tagged, keep = {}, {}
    for t, m in terms.items():
        (tagged if is_wl(t) and not is_protein(toks(t)) else keep)[t] = m

    # 2. Coverage in destination.
    cov = {t: coverage(t, kws, dest_neg_campaign) for t in tagged}
    routable = {t: m for t, m in tagged.items() if cov[t][0] is not None}

    # 3. Candidate phrase negatives: n-grams of routable terms that never occur in a kept term
    #    or in a source keyword, and contain no protein token.
    keep_toks = [toks(t) for t in keep] + [toks(k) for k in src_kw]
    cands = set()
    for t in routable:
        tt = toks(t)
        for n in range(2, 8):
            for i in range(len(tt) - n + 1):
                g = tt[i:i + n]
                if g[0] in FILLER - {"best"} or g[-1] in FILLER or any(w.isdigit() for w in g):
                    continue
                if not is_protein(g):
                    cands.add(" ".join(g))
    safe = {c for c in cands if not any(contains_seq(kt, toks(c)) for kt in keep_toks)}

    # 4. Greedy cover of routable spend, preferring shorter (broader) negatives.
    remaining = {t for t, m in routable.items() if m["cost_micros"] / 1e6 >= args.min_spend}
    chosen = []
    while remaining:
        def gain(c):
            return sum(routable[t]["cost_micros"] for t in remaining if contains_seq(toks(t), toks(c)))
        best = max(safe, key=lambda c: (gain(c), -len(toks(c))), default=None)
        if best is None or gain(best) == 0:
            break
        hit = {t for t in remaining if contains_seq(toks(t), toks(best))}
        chosen.append((best, hit))
        remaining -= hit

    gbp = lambda m: m["cost_micros"] / 1e6

    if args.validate:
        # Validate a curated list: [text, PHRASE|EXACT] per line in a JSON list.
        print(f"{'negative':52}{'blocks':>7}{'£':>7}{'conv':>6}{'collateral £':>14}  dest coverage of blocked spend")
        for neg, mt in json.load(open(args.validate)):
            hit = [t for t in terms if neg_blocks(toks(t), neg, mt)]
            bad = [t for t in hit if t in keep]
            kw_conflict = [k for k in src_kw if neg_blocks(toks(k), neg, mt)]
            hit_cov = [t for t in hit if t in tagged]
            covered = sum(gbp(terms[t]) for t in hit_cov if cov[t][0] is not None)
            sure = sum(gbp(terms[t]) for t in hit_cov if cov[t][0] is not None and "near match" not in cov[t][1])
            spend = sum(gbp(terms[t]) for t in hit)
            label = f"{'[' + neg + ']' if mt == 'EXACT' else chr(34) + neg + chr(34)}"
            print(f"{label:52}{len(hit):>7}{spend:>7.0f}{sum(terms[t]['conversions'] for t in hit):>6.1f}"
                  f"{sum(gbp(terms[t]) for t in bad):>14.0f}  confirmed £{sure:.0f} / likely £{covered - sure:.0f} / none £{spend - covered:.0f}"
                  + (f"  COLLATERAL: {bad[:3]}" if bad else "") + (f"  KW CONFLICT: {kw_conflict}" if kw_conflict else ""))
        return

    print(f"Source terms:{len(terms)}  tagged weight-loss/no-protein: {len(tagged)} "
          f"(£{sum(gbp(m) for m in tagged.values()):,.0f})  routable to {args.dest_campaign}: {len(routable)} "
          f"(£{sum(gbp(m) for m in routable.values()):,.0f})\n")
    print("PROPOSED PHRASE NEGATIVES (source campaign level)")
    for neg, hit in chosen:
        all_hit = [t for t in terms if contains_seq(toks(t), toks(neg))]
        spend = sum(gbp(terms[t]) for t in all_hit)
        conv = sum(terms[t]["conversions"] for t in all_hit)
        print(f'  "{neg}"  blocks {len(all_hit)} terms, £{spend:,.0f}, {conv:.1f} conv  e.g. {sorted(hit, key=lambda t: -gbp(terms[t]))[:3]}')
    print("\nTAGGED TERMS (>= min spend) and destination coverage")
    for t, m in sorted(tagged.items(), key=lambda x: -gbp(x[1])):
        if gbp(m) < args.min_spend:
            continue
        k, why = cov[t]
        dest = f"{k['ag']} / {k['match'].lower()} '{k['text']}' ({why})" if k else f"NOT ROUTABLE: {why}"
        print(f"  £{gbp(m):>6.0f} conv {m['conversions']:>4.1f}  {t:55} -> {dest}")


if __name__ == "__main__":
    main()
