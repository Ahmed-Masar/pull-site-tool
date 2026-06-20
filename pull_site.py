#!/usr/bin/env python3
"""
pull_site.py - Mirror a Framer-published site with HTTrack and clean it up
for offline/local hosting.

Usage:
    python3 pull_site.py <url> [project_name] [--output-dir DIR] [--serve] [--port N]

Example:
    python3 pull_site.py https://wedo-iq.framer.website/ wedo
"""
import argparse
import functools
import hashlib
import http.server
import os
import re
import subprocess
import sys
import threading
import unicodedata
import urllib.error
import urllib.request
from urllib.parse import urlsplit, unquote

SAFE_NAME_RE = re.compile(r'^[A-Za-z0-9._-]+$')
EXTERNAL_SCHEME_RE = re.compile(r'^(?:[a-zA-Z][a-zA-Z0-9+.-]*:|//|#)')
ATTR_RE = re.compile(r'(?P<attr>\bhref|\bsrc)\s*=\s*(?P<quote>["\'])(?P<value>[^"\']*)(?P=quote)')
FRAMER_BADGE_MARKER = '<div id="__framer-badge-container">'
FRAMER_EVENTS_SCRIPT_RE = re.compile(
    r'<script[^>]*\bsrc="https://events\.framer\.com[^"]*"[^>]*></script>',
    re.IGNORECASE,
)
FRAMER_SITEID_QUERY_RE = re.compile(r'[?&]framerSiteId=[^"\'&]*')
HTML_EXTS = {'.html', '.htm'}


def run_httrack(url, output_dir):
    cmd = ["httrack", url, "-O", output_dir, "-q", "-%v0", "-s2", "-f2"]
    print(f"[1/5] Running HTTrack: {' '.join(cmd)}")
    result = subprocess.run(cmd)
    return result.returncode == 0


def discover_mirror_root(output_dir):
    """Finds the actual site folder HTTrack creates inside output_dir (named after the host),
    as opposed to its own hts-cache/log files. Works without knowing the original URL."""
    for name in sorted(os.listdir(output_dir)):
        full = os.path.join(output_dir, name)
        if os.path.isdir(full) and name != "hts-cache":
            return full
    return output_dir


def fix_mojibake(s):
    try:
        return s.encode('latin1').decode('utf-8')
    except (UnicodeDecodeError, UnicodeEncodeError):
        return s


def slugify(name):
    base, ext = os.path.splitext(name)
    repaired = base
    for _ in range(2):
        candidate = fix_mojibake(repaired)
        if candidate == repaired:
            break
        repaired = candidate
    normalized = unicodedata.normalize('NFKD', repaired)
    ascii_str = normalized.encode('ascii', 'ignore').decode('ascii')
    ascii_str = re.sub(r'[^A-Za-z0-9]+', '-', ascii_str).strip('-').lower()
    if not ascii_str:
        h = hashlib.sha1(base.encode('utf-8', 'surrogateescape')).hexdigest()[:8]
        ascii_str = f"page-{h}"
    return ascii_str + ext.lower()


def sanitize_filenames(mirror_root):
    """Renames files with non-ASCII/unsafe names. Returns {old_abspath: new_abspath}."""
    renames = {}
    for dirpath, _dirnames, filenames in os.walk(mirror_root):
        used = set(filenames)
        for name in filenames:
            if SAFE_NAME_RE.match(name):
                continue
            new_name = slugify(name)
            candidate = new_name
            n = 2
            while candidate in used and candidate != name:
                base, ext = os.path.splitext(new_name)
                candidate = f"{base}-{n}{ext}"
                n += 1
            used.discard(name)
            used.add(candidate)
            old_abs = os.path.normpath(os.path.join(dirpath, name))
            new_abs = os.path.normpath(os.path.join(dirpath, candidate))
            os.rename(old_abs, new_abs)
            renames[old_abs] = new_abs
    return renames


def iter_html_files(mirror_root):
    for dirpath, _dirnames, filenames in os.walk(mirror_root):
        for name in filenames:
            if os.path.splitext(name)[1].lower() in HTML_EXTS:
                yield os.path.join(dirpath, name)


def resolve_local_target(html_path, value):
    """Returns the normalized absolute filesystem path a local href/src points to, or None if external/empty."""
    if not value or EXTERNAL_SCHEME_RE.match(value):
        return None
    path_part = value.split('#', 1)[0].split('?', 1)[0]
    if not path_part:
        return None
    decoded = unquote(path_part, errors='replace')
    base_dir = os.path.dirname(html_path)
    return os.path.normpath(os.path.join(base_dir, decoded))


def rewrite_links(mirror_root, renames):
    """Rewrites href/src attributes that point at renamed files. Returns count of links rewritten."""
    rewritten = 0
    for html_path in iter_html_files(mirror_root):
        with open(html_path, encoding='utf-8', errors='surrogateescape') as f:
            content = f.read()

        def replace(match):
            nonlocal rewritten
            value = match.group('value')
            target = resolve_local_target(html_path, value)
            if target is None or target not in renames:
                return match.group(0)
            new_target = renames[target]
            new_rel = os.path.relpath(new_target, os.path.dirname(html_path)).replace(os.sep, '/')
            suffix = ''
            if '?' in value.split('#', 1)[0]:
                suffix = '?' + value.split('#', 1)[0].split('?', 1)[1]
            if '#' in value:
                suffix += '#' + value.split('#', 1)[1]
            rewritten += 1
            quote = match.group('quote')
            return f"{match.group('attr')}={quote}{new_rel}{suffix}{quote}"

        new_content = ATTR_RE.sub(replace, content)
        if new_content != content:
            with open(html_path, 'w', encoding='utf-8', errors='surrogateescape') as f:
                f.write(new_content)
    return rewritten


def build_html_index(mirror_root):
    """Maps lowercased html basename (without extension) -> list of absolute file paths."""
    index = {}
    for html_path in iter_html_files(mirror_root):
        base = os.path.splitext(os.path.basename(html_path))[0].lower()
        index.setdefault(base, []).append(html_path)
    return index


def fix_spa_links(mirror_root):
    """Resolves extensionless SPA-style relative links (e.g. ./about) to their real .html
    file when exactly one match exists by basename. Returns (fixed_count, unresolved_list)."""
    index = build_html_index(mirror_root)
    fixed = 0
    unresolved = []

    for html_path in iter_html_files(mirror_root):
        with open(html_path, encoding='utf-8', errors='surrogateescape') as f:
            content = f.read()

        def replace(match):
            nonlocal fixed
            value = match.group('value')
            if not value or EXTERNAL_SCHEME_RE.match(value):
                return match.group(0)

            path_part, sep, rest = value.partition('#')
            anchor = ('#' + rest) if sep else ''
            query = ''
            if '?' in path_part:
                path_part, _, query_str = path_part.partition('?')
                query = '?' + query_str

            if path_part in ('', '.', './'):
                return match.group(0)
            if not (path_part.startswith('./') or '/' not in path_part):
                return match.group(0)
            last_segment = path_part.rstrip('/').split('/')[-1]
            if '.' in last_segment:
                return match.group(0)

            candidates = index.get(last_segment.lower(), [])
            rel_html = os.path.relpath(html_path, mirror_root)
            if len(candidates) != 1:
                reason = "no match" if not candidates else "ambiguous: " + ", ".join(
                    os.path.relpath(c, mirror_root) for c in candidates
                )
                unresolved.append((rel_html, value, reason))
                return match.group(0)

            target = candidates[0]
            new_rel = os.path.relpath(target, os.path.dirname(html_path)).replace(os.sep, '/')
            fixed += 1
            quote = match.group('quote')
            return f"{match.group('attr')}={quote}{new_rel}{query}{anchor}{quote}"

        new_content = ATTR_RE.sub(replace, content)
        if new_content != content:
            with open(html_path, 'w', encoding='utf-8', errors='surrogateescape') as f:
                f.write(new_content)

    return fixed, unresolved


def strip_balanced_div(content, marker):
    """Removes a <div ...>...</div> block starting at the given opening-tag marker, handling nested divs."""
    removed = 0
    while True:
        start = content.find(marker)
        if start == -1:
            break
        open_re = re.compile(r'<div\b', re.IGNORECASE)
        close_re = re.compile(r'</div\s*>', re.IGNORECASE)
        cursor = start + len(marker)
        depth = 1
        while depth > 0:
            next_open = open_re.search(content, cursor)
            next_close = close_re.search(content, cursor)
            if not next_close:
                break
            if next_open and next_open.start() < next_close.start():
                depth += 1
                cursor = next_open.end()
            else:
                depth -= 1
                cursor = next_close.end()
        content = content[:start] + content[cursor:]
        removed += 1
    return content, removed


def strip_framer_artifacts(mirror_root):
    """Removes the Framer watermark badge, tracking script, and framerSiteId query params. Returns counts."""
    badge_count = 0
    script_count = 0
    siteid_count = 0
    for html_path in iter_html_files(mirror_root):
        with open(html_path, encoding='utf-8', errors='surrogateescape') as f:
            content = f.read()
        original = content

        content, n = strip_balanced_div(content, FRAMER_BADGE_MARKER)
        badge_count += n

        content, n = FRAMER_EVENTS_SCRIPT_RE.subn('', content)
        script_count += n

        content, n = FRAMER_SITEID_QUERY_RE.subn('', content)
        siteid_count += n

        if content != original:
            with open(html_path, 'w', encoding='utf-8', errors='surrogateescape') as f:
                f.write(content)
    return badge_count, script_count, siteid_count


def validate_links(mirror_root):
    """Re-scans every html file for local hrefs/srcs that don't resolve to an existing file. Returns list of issues."""
    broken = []
    for html_path in iter_html_files(mirror_root):
        with open(html_path, encoding='utf-8', errors='surrogateescape') as f:
            content = f.read()
        for match in ATTR_RE.finditer(content):
            value = match.group('value')
            target = resolve_local_target(html_path, value)
            if target is None:
                continue
            if not os.path.exists(target):
                rel_html = os.path.relpath(html_path, mirror_root)
                broken.append((rel_html, value))
    return broken


def serve_directory(mirror_root, port=0):
    """Starts a background HTTP server for mirror_root. Caller is responsible for httpd.shutdown()."""
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=mirror_root)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    actual_port = httpd.server_address[1]
    return httpd, f"http://127.0.0.1:{actual_port}/"


def smoke_test(mirror_root, port=0):
    """Serves the mirror over HTTP locally and checks every page + local asset returns 200.
    Returns a {relative_path: status_or_error} dict; the temporary server is always shut down before returning."""
    httpd, base_url = serve_directory(mirror_root, port)
    try:
        checked = {}
        targets = set()
        for html_path in iter_html_files(mirror_root):
            rel = os.path.relpath(html_path, mirror_root).replace(os.sep, '/')
            targets.add(rel)
            with open(html_path, encoding='utf-8', errors='surrogateescape') as f:
                content = f.read()
            for match in ATTR_RE.finditer(content):
                target = resolve_local_target(html_path, match.group('value'))
                if target and os.path.isfile(target):
                    targets.add(os.path.relpath(target, mirror_root).replace(os.sep, '/'))

        for rel in sorted(targets):
            url = base_url + rel
            try:
                with urllib.request.urlopen(url, timeout=5) as resp:
                    resp.read()
                    checked[rel] = resp.status
            except urllib.error.HTTPError as e:
                checked[rel] = e.code
            except Exception as e:
                checked[rel] = str(e)
        return checked
    finally:
        httpd.shutdown()


def run_pipeline(url, project_name=None, base_dir=None, port=0, log=lambda msg: None):
    """Runs the full download+cleanup pipeline and returns a JSON-serializable report dict.
    Used by both the CLI (main) and the Flask API (app.py)."""
    base_dir = base_dir or os.getcwd()
    project_name = project_name or slugify(urlsplit(url).netloc)
    output_dir = os.path.join(base_dir, project_name)
    os.makedirs(output_dir, exist_ok=True)

    log("[1/6] Running HTTrack download...")
    if not run_httrack(url, output_dir):
        return {
            "success": False,
            "stage": "download",
            "error": f"HTTrack failed for {url}",
            "project_name": project_name,
            "url": url,
        }

    mirror_root = discover_mirror_root(output_dir)

    log("[2/6] Sanitizing filenames...")
    renames = sanitize_filenames(mirror_root)

    log("[3/6] Rewriting internal links...")
    rewritten = rewrite_links(mirror_root, renames)

    log("[4/6] Fixing SPA-style extensionless links...")
    spa_fixed, spa_unresolved = fix_spa_links(mirror_root)

    log("[5/6] Removing Framer-specific elements...")
    badge_count, script_count, siteid_count = strip_framer_artifacts(mirror_root)

    log("[6/6] Validating links and running smoke test...")
    broken = validate_links(mirror_root)
    checked = smoke_test(mirror_root, port)
    failing = {k: v for k, v in checked.items() if v != 200}

    return {
        "success": not (broken or failing or spa_unresolved),
        "stage": "done",
        "project_name": project_name,
        "url": url,
        "mirror_root": mirror_root,
        "renames": {
            os.path.relpath(old, mirror_root): os.path.relpath(new, mirror_root)
            for old, new in renames.items()
        },
        "links_rewritten": rewritten,
        "spa_links_fixed": spa_fixed,
        "spa_links_unresolved": [
            {"file": f, "value": v, "reason": r} for f, v, r in spa_unresolved
        ],
        "framer_artifacts_removed": {
            "badge": badge_count,
            "tracking_script": script_count,
            "framer_siteid_params": siteid_count,
        },
        "broken_links": [{"file": f, "value": v} for f, v in broken],
        "smoke_test": checked,
    }


def print_report(report):
    print("\n" + "=" * 60)
    print(f" PULL SITE REPORT: {report['project_name']}")
    print("=" * 60)
    if report["stage"] == "download":
        print(f"[1] HTTrack download:        FAILED ({report['error']})")
        print("=" * 60)
        return
    print(f"[1] HTTrack download:        SUCCESS ({report['url']})")
    print(f"    Mirror root:             {report['mirror_root']}")
    print(f"[2] Filenames sanitized:     {len(report['renames'])}")
    for old, new in report["renames"].items():
        print(f"    - {old} -> {new}")
    print(f"[3] Links rewritten:         {report['links_rewritten']}")
    print(f"[4] SPA links fixed:         {report['spa_links_fixed']}")
    print(f"[4] SPA links unresolved:    {len(report['spa_links_unresolved'])}")
    for item in report["spa_links_unresolved"]:
        print(f"    - {item['file']}: {item['value']} ({item['reason']})")
    fa = report["framer_artifacts_removed"]
    print(f"[5] Framer artifacts removed: badge={fa['badge']}, tracking-script={fa['tracking_script']}, framerSiteId-params={fa['framer_siteid_params']}")
    if report["broken_links"]:
        print(f"[6] Link validation:         {len(report['broken_links'])} BROKEN LINK(S) FOUND")
        for item in report["broken_links"]:
            print(f"    - {item['file']}: {item['value']}")
    else:
        print("[6] Link validation:         ALL LINKS VALID")
    failing = {k: v for k, v in report["smoke_test"].items() if v != 200}
    if failing:
        print(f"[7] Smoke test (local HTTP): {len(failing)} resource(s) FAILED")
        for rel, status in failing.items():
            print(f"    - {rel}: {status}")
    else:
        print(f"[7] Smoke test (local HTTP): ALL {len(report['smoke_test'])} resources returned 200 OK")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Download and clean a Framer site with HTTrack.")
    parser.add_argument("url")
    parser.add_argument("project_name", nargs="?", default=None)
    parser.add_argument("--output-dir", default=os.getcwd(), help="Base directory to create the project folder in (default: cwd)")
    parser.add_argument("--serve", action="store_true", help="Keep a local server running after cleanup for manual visual testing")
    parser.add_argument("--port", type=int, default=0, help="Port for --serve (default: auto-pick a free port)")
    args = parser.parse_args()

    report = run_pipeline(args.url, args.project_name, args.output_dir, args.port, log=print)
    print_report(report)

    if report["stage"] == "download":
        sys.exit(1)

    if args.serve:
        httpd, base_url = serve_directory(report["mirror_root"], args.port)
        print(f"\nServer running at {base_url} — open it in a browser to visually verify the site.")
        print("Press Enter to stop the server.")
        input()
        httpd.shutdown()
    else:
        print(f"\nTo preview manually:\n  cd \"{report['mirror_root']}\" && python3 -m http.server 8000\n  then open http://localhost:8000/")

    sys.exit(0 if report["success"] else 1)


if __name__ == "__main__":
    main()
