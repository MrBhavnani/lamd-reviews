"""Copy reviews.json into carousel.html's built-in fallback snapshot. Run after every refresh."""
import json, re, pathlib
root = pathlib.Path(__file__).parent
data = json.loads((root / "reviews.json").read_text(encoding="utf-8"))
snap = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
p = root / "carousel.html"
html = p.read_text(encoding="utf-8")
new, n = re.subn(r"/\*SNAPSHOT\*/.*?/\*END\*/", lambda m: "/*SNAPSHOT*/" + snap + "/*END*/", html, flags=re.S)
assert n == 1, "snapshot marker not found"
p.write_text(new, encoding="utf-8")
print("snapshot updated:", len(data["reviews"]), "reviews")
