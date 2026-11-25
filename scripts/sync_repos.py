"""표지 현황(ahnboat_lab_cover_status)의 data/repos.json에 새로 잡힌 저장소를 repos.txt 끝에 더한다.
빼고 싶은 저장소는 repos_skip.txt에 적는다. 회사 저장소 newsreporter_*는 늘 뺀다.
사용: python3 scripts/sync_repos.py [repos.json 경로]   (경로가 없으면 GitHub에서 받는다)
"""
import json, os, sys, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
URL = "https://raw.githubusercontent.com/adminmrobo/ahnboat_lab_cover_status/%s/data/repos.json" % (os.environ.get("SRC_SHA") or "main")

def lines(path):
    if not os.path.exists(path):
        return []
    return [l.strip() for l in open(path, encoding="utf-8") if l.strip() and not l.strip().startswith("#")]

def main():
    if len(sys.argv) > 1:
        data = json.load(open(sys.argv[1], encoding="utf-8"))
    else:
        with urllib.request.urlopen(URL, timeout=120) as r:
            data = json.load(r)
    have = set(lines(os.path.join(HERE, "repos.txt")))
    skip = set(lines(os.path.join(HERE, "repos_skip.txt")))
    new = []
    for item in data:
        r = item.get("repo")
        if not r or r in have or r in skip or r.startswith("newsreporter"):
            continue
        if item.get("has_pages") is False and not item.get("covers"):
            continue
        new.append(r); have.add(r)
    if new:
        with open(os.path.join(HERE, "repos.txt"), "a", encoding="utf-8") as f:
            for r in new:
                f.write(r + "\n")
    print("새 저장소 %d곳: %s" % (len(new), ", ".join(new) or "없음"))

if __name__ == "__main__":
    main()
