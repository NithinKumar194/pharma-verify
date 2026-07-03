#!/usr/bin/env python3
"""
gen_full_qr.py — commission + render print-ready QR codes that carry EVERY field.

For the QR-only product: encode GTIN, Serial, Batch, MFG, EXP, MRP and Licence into one
GS1 Digital Link, so verify_qr.py gets all fields from a single decode (no OCR needed).

The standard GS1 AIs (01 GTIN, 10 batch, 21 serial, 11 mfg, 17 exp) go in the path/query so
any GS1 scanner still reads them; MRP / licence / product have no clean GS1 AI, so they ride as
extra query params (mrp=, lic=, pn=) that our reader understands and other scanners ignore.

Two modes:
  --api URL     commission real unique serials via serial_api.py (recommended for production)
  --no-api      generate random serials locally for a quick demo (no server needed)

Examples:
  # demo (no server): make 5 fully-encoded packs + mock pack images to scan
  python gen_full_qr.py --no-api --gtin 08901302207789 --batch DOBS4376 \
      --mfg 2026-02 --exp 2030-01 --mrp 32.12 --lic KA/28/2009 \
      --product "Dolo 650" --qty 5 --mock --out qr_full_batch

  # production (server running): commission + render, then activate
  uvicorn serial_api:app --port 8077            # terminal 1
  python gen_full_qr.py --api http://127.0.0.1:8077 --gtin 08901302207789 \
      --batch DOBS4376 --mfg 2026-02 --exp 2030-01 --mrp 32.12 --lic KA/28/2009 \
      --product "Dolo 650" --qty 100 --host id.microlabsltd.com --out print_batch --activate

Requires:  pip install segno   (and opencv-python + numpy only if --mock)
"""
import argparse, csv, json, os, re, secrets, socket, sys, urllib.request

try:
    import segno
except ImportError:
    sys.exit("Missing segno. Install:  pip install segno")

_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"          # no 0/O/1/I/L confusables


def lan_ip():
    """This PC's LAN IP (what phones on the same Wi-Fi can reach)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))                      # no traffic sent; just picks the outbound iface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _yymmdd(ym, day="01"):
    m = re.fullmatch(r"(\d{4})-(\d{2})", ym or "")
    if not m:
        return None
    return f"{m.group(1)[2:]}{m.group(2)}{day}"


def gen_serial(n=16):
    return "".join(secrets.choice(_ALPHABET) for _ in range(n))


def digital_link(host, gtin, batch, serial, mfg_ym, exp_ym, mrp, lic, product):
    """Build the QR link to the /p details page on our server. Works with a LAN IP
    (http://192.168.x.x:8077, same-Wi-Fi) or a public tunnel domain
    (https://xxx.trycloudflare.com, works from anywhere)."""
    is_local = re.match(r"^\d+\.\d+\.\d+\.\d+(:\d+)?$|^localhost(:\d+)?$", host)
    scheme = "http" if is_local else "https"
    url = f"{scheme}://{host}/p/01/{gtin}/10/{batch}/21/{serial}"
    q = []
    if mfg_ym and _yymmdd(mfg_ym):
        q.append(f"11={_yymmdd(mfg_ym)}")
    if exp_ym and _yymmdd(exp_ym):
        q.append(f"17={_yymmdd(exp_ym)}")
    if mrp:
        q.append(f"mrp={mrp}")
    if lic:
        q.append(f"lic={urllib.request.quote(str(lic), safe='/')}")
    if product:
        q.append(f"pn={urllib.request.quote(str(product), safe='')}")
    return url + ("?" + "&".join(q) if q else "")


def api_post(base, path, payload):
    headers = {"Content-Type": "application/json"}
    key = os.environ.get("PHARMA_ADMIN_KEY", "").strip()
    if key:
        headers["X-API-Key"] = key                # manufacturer auth for secured servers
    req = urllib.request.Request(base.rstrip("/") + path, data=json.dumps(payload).encode(),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def make_mock(png_path, out_png, product, batch):
    """Render a simple 'pack' image (white canvas + the QR) for end-to-end testing."""
    import cv2, numpy as np
    q = cv2.imread(png_path)
    canvas = np.full((900, 1500, 3), 255, np.uint8)
    qh, qw = q.shape[:2]
    canvas[60:60 + qh, 1500 - qw - 60:1500 - 60] = q
    cv2.putText(canvas, str(product), (70, 230), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (25, 25, 25), 3)
    cv2.putText(canvas, f"B.No. {batch}", (70, 300), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (40, 40, 40), 2)
    cv2.imwrite(out_png, canvas)


def main():
    ap = argparse.ArgumentParser(description="Commission + render fully-encoded QR codes (QR-only product)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--api", help="serial_api base URL (commission real serials)")
    g.add_argument("--no-api", action="store_true", help="generate serials locally (demo, no server)")
    ap.add_argument("--gtin", required=True)
    ap.add_argument("--batch", required=True)
    ap.add_argument("--mfg", help="YYYY-MM")
    ap.add_argument("--exp", help="YYYY-MM")
    ap.add_argument("--mrp", help="e.g. 32.12")
    ap.add_argument("--lic", help="manufacturing licence no., e.g. KA/28/2009")
    ap.add_argument("--product", help="product / brand name")
    ap.add_argument("--qty", type=int, default=5)
    ap.add_argument("--host", default="auto",
                    help="QR link host. 'auto' = this PC's LAN IP + :8077 (phones on same Wi-Fi can open it)")
    ap.add_argument("--out", default="qr_full_batch")
    ap.add_argument("--scale", type=int, default=12)
    ap.add_argument("--error", default="m", choices=["l", "m", "q", "h"])
    ap.add_argument("--mock", action="store_true", help="also render mock pack images for testing")
    ap.add_argument("--style", default="link", choices=["link", "text"],
                    help="link = QR opens the details web page (needs server reachable); "
                         "text = details are INSIDE the QR, phone shows them instantly, offline, no server")
    ap.add_argument("--activate", action="store_true", help="(--api) activate the batch after rendering")
    args = ap.parse_args()

    if args.host == "auto":
        port = "8077"
        if args.api:
            m = re.search(r":(\d+)", args.api)
            if m:
                port = m.group(1)
        args.host = f"{lan_ip()}:{port}"
        print(f"[host] QR links will open http://{args.host}/p/... on this PC "
              f"(phone must be on the same Wi-Fi; server must be started with --host 0.0.0.0)")

    os.makedirs(args.out, exist_ok=True)

    if args.api:
        print(f"Commissioning {args.qty} serials for batch {args.batch} via {args.api} ...")
        extra = json.dumps({"mrp": args.mrp, "lic": args.lic, "pn": args.product, "host": args.host})
        resp = api_post(args.api, "/commission", {
            "gtin": args.gtin, "batch_no": args.batch, "qty": args.qty,
            "mfg_date": args.mfg, "exp_date": args.exp,
            "product_name": args.product, "link": extra})
        batch_id, serials = resp["batch_id"], resp["serials"]
        print(f"  batch_id={batch_id}; rendering {len(serials)} QR codes -> {args.out}/")
    else:
        serials = [gen_serial() for _ in range(args.qty)]
        batch_id = "LOCAL-DEMO"
        print(f"[demo] generated {len(serials)} local serials (no server) -> {args.out}/")

    manifest = os.path.join(args.out, f"{args.batch}_manifest.csv")
    with open(manifest, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["serial", "gtin", "batch", "mfg", "exp", "mrp", "lic", "url", "png", "mock"])
        for sn in serials:
            if args.style == "text":
                # Self-contained: phone shows these details instantly on scan — offline, no server.
                lines = [f"PRODUCT: {args.product or '-'}",
                         f"GTIN/UPIN: {args.gtin}",
                         f"BATCH: {args.batch}",
                         f"MFG: {args.mfg or '-'}",
                         f"EXP: {args.exp or '-'}",
                         f"MRP: Rs.{args.mrp or '-'}",
                         f"LIC.NO: {args.lic or '-'}",
                         f"SERIAL: {sn}"]
                url = "\n".join(lines)
            else:
                url = digital_link(args.host, args.gtin, args.batch, sn, args.mfg, args.exp,
                                   args.mrp, args.lic, args.product)
            png = os.path.join(args.out, f"{args.batch}_{sn}.png")
            segno.make_qr(url, error=args.error).save(png, scale=args.scale, border=4)
            mock = ""
            if args.mock:
                mock = os.path.join(args.out, f"pack_{args.batch}_{sn}.png")
                make_mock(png, mock, args.product or "Medicine", args.batch)
            w.writerow([sn, args.gtin, args.batch, args.mfg, args.exp, args.mrp, args.lic, url, png, mock])
    print(f"  manifest: {manifest}")
    print(f"  sample link: {digital_link(args.host, args.gtin, args.batch, serials[0], args.mfg, args.exp, args.mrp, args.lic, args.product)}")

    if args.api and args.activate:
        api_post(args.api, "/activate", {"batch_id": batch_id})
        print(f"  batch {batch_id} ACTIVATED")
    elif args.no_api and args.activate:
        print("  (--activate ignored in --no-api mode; no server to update)")


if __name__ == "__main__":
    main()