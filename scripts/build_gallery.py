#!/usr/bin/env python3
"""연구실 식구 갤러리 데이터 만들기.

adminmrobo 공개 저장소들을 미리 내려받아 둔 폴더(WORKDIR)를 훑어
  - 이미지 목록(저장소·경로·git 파일 해시)
  - 그림이 저장소에 처음 올라간 날짜(생성일 대용)
  - 그림을 만든 프롬프트(JSON 명세·Genspark 요청서 md)
  - 콘텐츠 이름·사이트 주소(표지 현황 catalog.json, cover_manifest.json)
를 모아 data/gallery.json, data/prompts.json 을 만든다.

준비(저장소마다):
  git clone --filter=blob:none --no-checkout https://github.com/adminmrobo/<repo>.git WORKDIR/<repo>
  git -C WORKDIR/<repo> sparse-checkout set --no-cone "*.md" "*.json"
  git -C WORKDIR/<repo> checkout
사용:
  python3 scripts/build_gallery.py WORKDIR
"""
import json, os, re, subprocess, sys, urllib.parse
from collections import defaultdict

OWNER = "adminmrobo"
IMG_RE = re.compile(r"\.(png|jpe?g|webp|gif)$", re.I)
SKIP_REPOS = {"ahnboat_lab_gallery"}
SKIP_PATH = re.compile(r"(^|/)(node_modules|\.git)/")

# ---------------- 캐릭터 사전 ----------------
CHARS = [
    # key, 한국어, 경로 토큰, 글 속 단어(정규식)
    ("doctor", "박사", {"doctor", "doc", "prof", "professor", "dr"},
     r"박사|\bthe Professor\b|chibi (?:male )?scientist|\bscientist\b"),
    ("haru", "조수 하루", {"haru", "asst", "assistant"},
     r"하루|조수|\bHaru\b|pink (?:hair|buns)|girl researcher with pink|lab assistant"),
    ("fruitfly", "초파리 연구원", {"fly", "fruitfly", "fruit"},
     r"초파리|fruit ?fly"),
    ("jupanbot", "주판봇", {"jupanbot", "robo", "robot", "ababot", "jupan"},
     r"주판봇|JupanBot|AbaBot|CRT(?:-monitor)? robot|beige (?:CRT|boxy|box)|boxy beige|abacus"),
    ("sia", "인턴 시아", {"sia"},
     r"시아|\bSia\b|jet-black hair"),
    ("moa", "인턴 모아", {"moa"},
     r"모아|\bMoa\b|ash-beige"),
    ("x1", "X1", {"x1"}, r"\bX1\b"),
    ("x2", "X2", {"x2"}, r"\bX2\b"),
    ("x3", "X3", {"x3"}, r"\bX3\b"),
]
MONEY_RE = re.compile(r"money[ _\-]?robo|머니\s?로보", re.I)
CHAR_RX = {k: re.compile(rx, re.I) for k, _, _, rx in CHARS}
X_ANY = re.compile(r"\bX1\s*[~·,～-]\s*X3\b|X1~X3|미공개 (?:응원단|캐릭터)|unrevealed", re.I)
FOUR = re.compile(r"네 캐릭터|four characters|4 characters", re.I)

def path_tokens(path):
    parts = path.lower().split("/")
    toks = set()
    for p in parts[:-1]:
        toks.add(p)
    base = os.path.splitext(parts[-1])[0]
    toks |= set(t for t in re.split(r"[_\-.\s]+", base) if t)
    return toks

def chars_from_path(path):
    if MONEY_RE.search(path):
        return {"money_robo"}
    toks = path_tokens(path)
    out = set()
    for k, _, ptoks, _ in CHARS:
        if toks & ptoks:
            out.add(k)
    # docs/ 폴더의 doc 오탐 방지
    if "doctor" in out and not (toks & {"doctor", "prof", "professor", "dr"}):
        base = os.path.basename(path).lower()
        if not re.match(r"(doc|prof)[_\-]", base):
            out.discard("doctor")
    return out

def chars_from_text(text):
    out = set()
    if not text:
        return out
    if MONEY_RE.search(text):
        out.add("money_robo")
    for k, rx in CHAR_RX.items():
        if rx.search(text):
            out.add(k)
    if X_ANY.search(text):
        out |= {"x1", "x2", "x3"}
    if FOUR.search(text):
        out |= {"doctor", "haru", "fruitfly", "jupanbot"}
    return out

JSON_CHAR_MAP = {"doctor": "doctor", "professor": "doctor", "prof": "doctor", "doc": "doctor",
                 "haru": "haru", "assistant": "haru", "asst": "haru",
                 "fruitfly": "fruitfly", "fly": "fruitfly",
                 "jupanbot": "jupanbot", "robo": "jupanbot", "robot": "jupanbot",
                 "sia": "sia", "moa": "moa", "x1": "x1", "x2": "x2", "x3": "x3",
                 "money_robo": "money_robo", "moneyrobo": "money_robo"}

# ---------------- 종류 ----------------
def kind_of(path):
    p = path.lower()
    b = os.path.basename(p)
    if re.search(r"(^|[/_\-])og([_\-/]|\d)|(^|[/_\-])thumb", p) or "/og/" in p:
        return "crop"
    if "cover" in b or "표지" in b:
        return "cover"
    if re.search(r"motion|sprite|fight|turn_|_walk|_run|/frames?/", p):
        return "motion"
    if re.search(r"sheet|_ref\b|refs?/|setting|turnaround|character_core", p):
        return "sheet"
    if re.search(r"(^|[/_])e\d{2}([_.]|$)|expr|expression|/images/(doctor|haru|fruitfly|jupanbot|sia|moa|x1|x2|x3)/", p):
        return "expression"
    if re.search(r"(^|[_/])(sc|s\d|scene|ty|pr|cut|panel|comic|try|f\d)[_\-]?\d*", b) or "scene" in p:
        return "scene"
    if re.search(r"av_|avatar|chars?/|/char/|pose", p):
        return "expression"
    return "etc"

KIND_KO = {"cover": "표지", "scene": "장면", "expression": "표정·포즈", "motion": "모션",
           "sheet": "설정 시트", "crop": "썸네일·OG", "etc": "기타"}

# ---------------- git ----------------
def git(repo_dir, *args):
    r = subprocess.run(["git", "-C", repo_dir, *args], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""

def list_images(repo_dir):
    out = []
    for line in git(repo_dir, "ls-tree", "-r", "HEAD").splitlines():
        meta, _, path = line.partition("\t")
        if IMG_RE.search(path) and not SKIP_PATH.search(path):
            out.append((path, meta.split()[2]))
    return out

def added_dates(repo_dir):
    dates = {}
    d = None
    for line in git(repo_dir, "log", "--diff-filter=A", "--name-only", "--no-renames",
                    "--format=@@%cI", "HEAD").splitlines():
        if line.startswith("@@"):
            d = line[2:]
        elif line.strip() and IMG_RE.search(line):
            dates[line] = d  # 뒤로 갈수록 오래된 커밋 → 마지막 값이 처음 올린 날
    return dates

def branch_of(repo_dir):
    b = git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD").strip()
    return b if b and b != "HEAD" else "main"

# ---------------- 프롬프트: JSON ----------------
PROMPT_KEYS = ("prompt_en", "prompt", "prompt_ko")
IMG_KEYS = ("image", "image_file", "file", "sheet", "sheet_file", "ref_frame", "img", "src", "path", "out")

def walk_json(x, found, ctx_chars=None):
    if isinstance(x, dict):
        prompt = next((x[k] for k in PROMPT_KEYS if isinstance(x.get(k), str) and len(x[k]) > 20), None)
        if prompt:
            paths = []
            for k in IMG_KEYS:
                v = x.get(k)
                if isinstance(v, str) and IMG_RE.search(v):
                    paths.append(v)
                elif isinstance(v, list):
                    paths += [s for s in v if isinstance(s, str) and IMG_RE.search(s)]
            cuts = []
            for k in ("frames", "panels", "cuts"):
                v = x.get(k)
                if isinstance(v, list):
                    cuts += [c for c in v if isinstance(c, str) and IMG_RE.search(c)]
            ids = [x.get("id")] if isinstance(x.get("id"), str) else []
            if isinstance(x.get("ids"), list):
                ids += [i for i in x["ids"] if isinstance(i, str)]
            chars = set()
            for k in ("characters", "who", "character", "who_list"):
                v = x.get(k)
                vs = v if isinstance(v, list) else [v]
                for s in vs:
                    if isinstance(s, str):
                        c = JSON_CHAR_MAP.get(s.lower().replace("-", "_"))
                        if c:
                            chars.add(c)
            title = next((x[k] for k in ("title", "ko", "label", "name") if isinstance(x.get(k), str)), "")
            neg = x.get("negative_en") if isinstance(x.get("negative_en"), str) else ""
            who = x.get("who") if isinstance(x.get("who"), str) else ""
            found.append({"paths": paths, "cuts": cuts, "who": who, "ids": ids, "prompt": prompt.strip(), "negative": neg.strip(),
                          "chars": chars, "title": title})
        for v in x.values():
            walk_json(v, found)
    elif isinstance(x, list):
        for v in x:
            walk_json(v, found)

# ---------------- 프롬프트: Markdown ----------------
FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.S)
HEAD = re.compile(r"^(#{1,6})\s+(.*)$", re.M)
SAVE_RE = re.compile(r"(?:저장(?:\s*이름)?|파일|키(?:\s*·\s*판)?|save as|file)\s*[:：|]?\s*\**`?([A-Za-z0-9_\-]+)(?:\.(?:png|webp|jpe?g))?", re.I)

def ascii_ratio(s):
    if not s:
        return 0
    return sum(1 for c in s if ord(c) < 128) / len(s)

def best_block(blocks):
    eng = [b for b in blocks if ascii_ratio(b) > 0.85 and len(b) > 60]
    pool = eng or [b for b in blocks if len(b) > 40]
    return max(pool, key=len).strip() if pool else ""

def parse_md(text, stems, docname):
    """[(stem, prompt, section_text)]"""
    res = []
    heads = list(HEAD.finditer(text))
    bounds = [(m.start(), m.group(2)) for m in heads] or [(0, "")]
    if heads and heads[0].start() > 0:
        bounds = [(0, "")] + bounds
    sections = []
    for i, (st, h) in enumerate(bounds):
        en = bounds[i + 1][0] if i + 1 < len(bounds) else len(text)
        sections.append((h, text[st:en]))

    def stems_in(s):
        hits = []
        for stem in stems:
            if re.search(r"(?<![\w])" + re.escape(stem) + r"(?![\w])", s, re.I):
                hits.append(stem)
        # 긴 이름 우선(겹침 제거)
        hits.sort(key=len, reverse=True)
        keep = []
        for h in hits:
            if not any(h != k and h in k for k in keep):
                keep.append(h)
        # 글에 나온 순서대로
        keep.sort(key=lambda k: s.lower().find(k.lower()))
        return keep

    single_doc = len([s for s in sections if FENCE.search(s[1])]) <= 2
    for h, body in sections:
        blocks = FENCE.findall(body)
        if not blocks:
            continue
        keys = stems_in(h)
        if not keys:
            pre = body.split("```", 1)[0]
            keys = [m.group(1) for m in SAVE_RE.finditer(pre) if m.group(1) in stems]
            if not keys:
                k2 = stems_in(pre)
                if len(k2) == 1:
                    keys = k2
        if not keys and single_doc:
            k2 = stems_in(os.path.basename(docname)) or stems_in(text.split("```", 1)[0])
            if len(k2) == 1:
                keys = k2
        if not keys:
            continue
        eng = [b.strip() for b in blocks if ascii_ratio(b) > 0.85 and len(b) > 60]
        if len(keys) > 1 and len(eng) == len(keys):
            for k, b in zip(keys, eng):
                res.append((k, b, h + "\n" + body))
        elif len(keys) >= 1:
            p = best_block(blocks)
            if p:
                for k in keys[:1] if len(keys) > 1 else keys:
                    res.append((k, p, h + "\n" + body))
    return res

# ---------------- 콘텐츠 이름 ----------------
def load_catalog(workdir):
    p = os.path.join(workdir, "ahnboat_lab_cover_status", "data", "catalog.json")
    by_repo = defaultdict(list)
    if os.path.exists(p):
        for it in json.load(open(p, encoding="utf-8")).get("items", []):
            u = (it.get("repo_url") or "").rstrip("/")
            if u.startswith("https://github.com/%s/" % OWNER):
                by_repo[u.split("/")[-1]].append(it)
    return by_repo

def repo_title(workdir, repo, catalog):
    items = catalog.get(repo)
    if items:
        it = items[0]
        return it.get("title", repo), it.get("site_url", ""), it.get("cat", ""), it.get("sub") or it.get("mid", "")
    cm = os.path.join(workdir, repo, "cover_manifest.json")
    if os.path.exists(cm):
        try:
            d = json.load(open(cm, encoding="utf-8"))
            return d.get("site_title") or d.get("title_ko") or repo, d.get("site_url", ""), "", ""
        except Exception:
            pass
    rd = os.path.join(workdir, repo, "README.md")
    if os.path.exists(rd):
        m = re.search(r"^#\s+(.+)$", open(rd, encoding="utf-8", errors="ignore").read(), re.M)
        if m:
            return re.sub(r"[*`]", "", m.group(1)).strip(), "", "", ""
    return repo, "", "", ""

# ---------------- 본체 ----------------
def main(workdir, outdir):
    catalog = load_catalog(workdir)
    by_hash = {}
    prompts = {}
    repos = sorted(r for r in os.listdir(workdir)
                   if os.path.isdir(os.path.join(workdir, r, ".git")) and r not in SKIP_REPOS
                   and not r.startswith("newsreporter"))
    repo_meta = {}
    for repo in repos:
        rd = os.path.join(workdir, repo)
        imgs = list_images(rd)
        if not imgs:
            continue
        branch = branch_of(rd)
        dates = added_dates(rd)
        title, site, cat, sub = repo_title(workdir, repo, catalog)
        repo_meta[repo] = {"title": title, "site": site, "cat": cat, "sub": sub, "branch": branch}
        stems = sorted({re.sub(r"@\dx$", "", os.path.splitext(os.path.basename(p))[0]) for p, _ in imgs}, key=len, reverse=True)

        # JSON 명세
        jprompt_by_path, jprompt_by_stem, jcut_path, jcut_id = {}, {}, {}, {}
        for root, _, files in os.walk(rd):
            if "/.git" in root:
                continue
            for f in files:
                if not f.endswith(".json") or f in ("kg.json", "refs.json") or f.endswith(".srcmap.json"):
                    continue
                fp = os.path.join(root, f)
                try:
                    if os.path.getsize(fp) > 8_000_000:
                        continue
                    d = json.load(open(fp, encoding="utf-8"))
                except Exception:
                    continue
                found = []
                walk_json(d, found)
                rel = os.path.relpath(fp, rd)
                for e in found:
                    e["src"] = rel
                    keys = {os.path.splitext(os.path.basename(p))[0] for p in e["paths"]} | set(e["ids"])
                    for p in e["paths"]:
                        jprompt_by_path[os.path.normpath(p).lstrip("./")] = e
                    for c in e["cuts"]:
                        jcut_path[os.path.normpath(c).lstrip("./")] = e
                    if e["who"]:
                        for i in e["ids"]:
                            jcut_id[(e["who"].lower(), i)] = e
                    for k in keys:
                        if k in jprompt_by_stem and jprompt_by_stem[k] is not e:
                            jprompt_by_stem[k] = None  # 이름이 겹치면 이름으로는 잇지 않음
                        else:
                            jprompt_by_stem[k] = e
        # Markdown 요청서
        mprompt = {}
        tprompt = {}  # 표 한 줄로 적힌 지시문
        for root, _, files in os.walk(rd):
            if "/.git" in root:
                continue
            for f in files:
                if not f.endswith(".md"):
                    continue
                fp = os.path.join(root, f)
                try:
                    txt = open(fp, encoding="utf-8", errors="ignore").read()
                except Exception:
                    continue
                rel = os.path.relpath(fp, rd)
                stemset = set(stems)
                for line in txt.splitlines():
                    if not line.lstrip().startswith("|"):
                        continue
                    cells = [c.strip().strip("`*").strip() for c in line.strip().strip("|").split("|")]
                    keyc = [c for c in cells if c in stemset or os.path.splitext(os.path.basename(c))[0] in stemset]
                    if len(keyc) != 1:
                        continue
                    k = os.path.splitext(os.path.basename(keyc[0]))[0]
                    others = [c for c in cells if c != keyc[0] and len(c) > 25]
                    if not others:
                        continue
                    best = max(others, key=lambda c: (ascii_ratio(c) > 0.85, len(c)))
                    if k not in tprompt or len(best) > len(tprompt[k]["prompt"]):
                        tprompt[k] = {"prompt": best, "src": rel, "sec": line,
                                      "summary": ascii_ratio(best) <= 0.85}
                if "```" not in txt:
                    continue
                for stem, p, sec in parse_md(txt, stems, rel):
                    if stem not in mprompt or len(p) > len(mprompt[stem]["prompt"]):
                        mprompt[stem] = {"prompt": p, "src": rel, "sec": sec}

        for path, h in imgs:
            stem = os.path.splitext(os.path.basename(path))[0]
            stem = re.sub(r"@\dx$", "", stem)
            pinfo = None
            e = jprompt_by_path.get(path)
            if e is None:
                # 같은 폴더 구조가 아니어도 파일 이름으로
                e = jprompt_by_stem.get(stem)
            if e is not None and e["paths"] and path not in [os.path.normpath(p).lstrip("./") for p in e["paths"]] and kind_of(path) == "expression" and not chars_from_path(path) & chars_from_text(" ".join(e["paths"])):
                e = None  # 다른 캐릭터의 같은 이름 표정
            cut = False
            if e is None:
                e = jcut_path.get(path)
                if e is None:
                    parts = path.split("/")
                    pref = stem.split("_")[0]
                    for who in parts[:-1]:
                        if (who.lower(), pref) in jcut_id:
                            e = jcut_id[(who.lower(), pref)]
                            break
                cut = e is not None
            weak = e is not None and (len(e["prompt"]) < 160 or re.search(r"\bsee\b|참고|요청서", e["prompt"][-80:]))
            if e is not None and not (weak and (stem in mprompt or stem in tprompt)):
                pinfo = {"prompt": e["prompt"], "negative": e["negative"], "src": e["src"],
                         "chars": e["chars"], "title": e["title"], "sec": "", "cut": cut,
                         "sheet": (e["paths"] or [""])[0] if cut else ""}
            elif stem in mprompt:
                m = mprompt[stem]
                pinfo = {"prompt": m["prompt"], "negative": "", "src": m["src"], "chars": set(),
                         "title": "", "sec": m["sec"]}
            elif stem in tprompt:
                m = tprompt[stem]
                pinfo = {"prompt": m["prompt"], "negative": "", "src": m["src"], "chars": set(),
                         "title": "", "sec": m["sec"], "summary": m["summary"]}
            date = dates.get(path)
            occ = {"repo": repo, "path": path, "date": date}
            rec = by_hash.get(h)
            if rec is None:
                rec = by_hash[h] = {"hash": h, "occ": [], "pinfo": None}
            rec["occ"].append(occ)
            if pinfo and (rec["pinfo"] is None or (not rec["pinfo"]["prompt"] and pinfo["prompt"])):
                rec["pinfo"] = dict(pinfo, repo=repo)

    allpaths = {(o["repo"], o["path"]) for rec in by_hash.values() for o in rec["occ"]}
    png_stems = {(r, os.path.splitext(os.path.basename(p))[0]) for r, p in allpaths
                 if p.lower().endswith(".png") and kind_of(p) in ("cover", "scene")}
    items = []
    for h, rec in by_hash.items():
        # 같은 그림의 @2x(큰 판)가 있으면 작은 판은 뺀다
        if all((o["repo"], re.sub(r"(\.[A-Za-z]+)$", r"@2x\1", o["path"])) in allpaths for o in rec["occ"]):
            continue
        # 표지·장면은 같은 저장소에 같은 이름의 png(원본)가 있으면 webp·jpg 사본을 뺀다
        def has_png(o):
            if o["path"].lower().endswith(".png") or kind_of(o["path"]) not in ("cover", "scene"):
                return False
            st = os.path.splitext(os.path.basename(o["path"]))[0]
            return (o["repo"], st) in png_stems
        if all(has_png(o) for o in rec["occ"]):
            continue
        occ = sorted(rec["occ"], key=lambda o: (o["date"] or "9999", o["repo"], o["path"]))
        first = occ[0]
        # 원본: 프롬프트가 붙은 곳이 있으면 그곳을, 없으면 가장 먼저 올라간 곳
        origin = first
        pinfo = rec["pinfo"]
        kinds = [kind_of(o["path"]) for o in occ]
        kind = next((k for k in ("cover", "scene", "expression", "motion", "sheet") if k in kinds), kinds[0])
        chars = set()
        for o in occ:
            chars |= chars_from_path(o["path"])
        if pinfo:
            chars |= pinfo["chars"]
            chars |= chars_from_text(pinfo["prompt"])
            sec = pinfo.get("sec") or ""
            m = re.search(r"등장\s*[:：]\s*([^\n]+)", sec)
            if m:
                chars |= chars_from_text(m.group(1))
        if any(MONEY_RE.search(o["path"]) for o in occ):
            chars.add("money_robo")
            chars.discard("jupanbot")
        # 수록 범위: 캐릭터가 확인되거나, 표지·장면 그림
        if not chars and kind not in ("cover", "scene"):
            continue
        if kind == "crop":
            continue
        if all(o["repo"] == "ahnboat_lab_cover_status" for o in occ):
            continue  # 표지 현황 사이트의 썸네일 사본
        meta = repo_meta[origin["repo"]]
        iid = h[:12]
        if pinfo and pinfo["prompt"]:
            prompts[iid] = {"p": pinfo["prompt"], "n": pinfo.get("negative", ""),
                            "src": "%s/%s" % (pinfo["repo"], pinfo["src"])}
            if pinfo.get("summary"):
                prompts[iid]["sum"] = 1
            if pinfo.get("cut"):
                prompts[iid]["cut"] = pinfo.get("sheet") or True
        uses = []
        seen = set()
        for o in occ:
            if o["repo"] in seen:
                continue
            seen.add(o["repo"])
            uses.append(o["repo"])
        items.append({
            "id": iid,
            "f": os.path.basename(origin["path"]),
            "r": origin["repo"],
            "p": origin["path"],
            "b": meta["branch"],
            "d": (first["date"] or "")[:10],
            "k": kind,
            "c": sorted(chars),
            "t": (pinfo or {}).get("title", "") if pinfo else "",
            "hp": iid in prompts,
            "u": uses,
            "n": len(occ),
        })
    items.sort(key=lambda x: (x["d"] or "0000", x["r"], x["p"]), reverse=True)
    out = {
        "owner": OWNER,
        "generated": subprocess.run(["date", "+%Y-%m-%d"], capture_output=True, text=True).stdout.strip(),
        "characters": [{"key": k, "ko": ko} for k, ko, _, _ in CHARS] + [{"key": "money_robo", "ko": "MONEY-ROBO"}],
        "kinds": KIND_KO,
        "repos": {r: repo_meta[r] for r in sorted({i["r"] for i in items} | {u for i in items for u in i["u"]})},
        "items": items,
    }
    os.makedirs(outdir, exist_ok=True)
    json.dump(out, open(os.path.join(outdir, "gallery.json"), "w", encoding="utf-8"),
              ensure_ascii=False, separators=(",", ":"))
    json.dump(prompts, open(os.path.join(outdir, "prompts.json"), "w", encoding="utf-8"),
              ensure_ascii=False, separators=(",", ":"))
    print("images:", len(items), "with prompt:", len(prompts), "repos:", len(out["repos"]))

if __name__ == "__main__":
    wd = sys.argv[1]
    od = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
    main(wd, od)
