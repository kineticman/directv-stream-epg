import argparse
import csv
import json
import os
import re
import sys
from xml.dom import minidom
from xml.etree.ElementTree import Element, SubElement, tostring

def _imageserver_chlogo(resource_id: str, *, w: int = 120, h: int = 90) -> str:
    """Stable channel logo URL used by the DirecTV Stream web guide."""
    rid = (resource_id or "").strip()
    if not rid:
        return ""
    return f"https://dfwfis.prod.dtvcdn.com/catalog/image/imageserver/v1/service/channel/{rid}/chlogo-clb-guide/{w}/{h}"



def _norm_ccid(ccid_raw: str, callsign_token: str = "", auth_url: str = "") -> str:
    """
    Normalize DirecTV channel ids (ccid).

    Sources:
      - ccid column (sometimes "", "nan", or float-ish like "836.0")
      - callsign token like "TBSHD-1234.dfw.1080" -> 1234
      - auth_url querystring like "...channel/v1?ccid=1574&clientContext=..."
    """
    ccid_raw = (ccid_raw or "").strip()
    if ccid_raw and ccid_raw.lower() != "nan":
        if re.fullmatch(r"\d+(\.0+)?", ccid_raw):
            return str(int(float(ccid_raw)))
        return ccid_raw

    # try auth_url first
    au = (auth_url or "").strip()
    m = re.search(r"(?:\?|&)ccid=(\d+)(?:&|$)", au)
    if m:
        return m.group(1)

    # then try token
    tok = (callsign_token or "").strip()
    m = re.search(r"-(\d+)(?:[.\)]|$)", tok)
    return m.group(1) if m else ""


def _callsign_from_token(token: str) -> str:
    token = (token or "").strip()
    return token.split("-", 1)[0].strip() if token else ""


def _read_csv(path: str):
    # utf-8-sig handles BOMs (Excel / some dumps)
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _pretty_xml_bytes(root: Element) -> bytes:
    raw = tostring(root, encoding="utf-8", xml_declaration=True)
    dom = minidom.parseString(raw)
    pretty = dom.toprettyxml(indent="  ", encoding="utf-8")
    return b"\n".join([ln for ln in pretty.splitlines() if ln.strip()]) + b"\n"


def _m3u_escape_attr(val: str) -> str:
    val = "" if val is None else str(val)
    return val.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", " ")


def _truthy(val) -> bool:
    s = "" if val is None else str(val).strip().lower()
    return s in ("1", "true", "yes", "y", "t", "on")


def _pick_m3u_url(ch: dict, mode: str) -> str:
    deeplink = (ch.get("deeplink") or "").strip()
    manifest = (ch.get("manifest_url") or "").strip()
    fallback = (ch.get("fallback_url") or "").strip()

    if mode == "deeplink":
        return deeplink
    if mode == "manifest":
        return manifest or fallback
    if mode == "fallback":
        return fallback
    # best
    return deeplink or manifest or fallback


def _parse_args(argv):
    """
    Supports two calling conventions:

      Positional (legacy):
        build_channels_exports.py <playback.csv> <allchannels.csv> <out.json> <out.xml> <out.m3u>

      Named (from daily_refresh.py):
        build_channels_exports.py --allchannels <csv> --out-m3u <m3u> [--playback <csv>]
                                  [--out-json <json>] [--out-xml <xml>] [--only-m3u]
    """
    p = argparse.ArgumentParser(
        prog="build_channels_exports.py",
        add_help=True,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # Positional args (optional now — use nargs='?' so named form works)
    p.add_argument("playback_csv", nargs="?", default="", help="playback_map.csv (positional)")
    p.add_argument("allchannels_csv_pos", nargs="?", default="", help="allchannels_map.csv (positional)")
    p.add_argument("out_json_pos", nargs="?", default="", help="output JSON file (positional)")
    p.add_argument("out_xml_pos", nargs="?", default="", help="output XMLTV channels file (positional)")
    p.add_argument("out_m3u_pos", nargs="?", default="", help="output M3U file (positional)")

    # Named args (preferred from daily_refresh)
    p.add_argument("--allchannels", default="", help="allchannels_map.csv")
    p.add_argument("--playback", default="", help="playback_map.csv (optional)")
    p.add_argument("--out-json", default="", dest="out_json_flag", help="output JSON file")
    p.add_argument("--out-xml", default="", dest="out_xml_flag", help="output XMLTV channels file")
    p.add_argument("--out-m3u", default="", dest="out_m3u_flag", help="output M3U file")
    p.add_argument("--only-m3u", action="store_true", help="Only produce the M3U (skip JSON + XML)")
    p.add_argument("--url-mode", choices=["deeplink", "manifest", "fallback", "best"], default=None,
                   help="Alias for --m3u-url-mode")

    p.add_argument(
        "--m3u-url-mode",
        choices=["deeplink", "manifest", "fallback", "best"],
        default="deeplink",
        help="Which URL to emit as the M3U playlist URL for each channel",
    )
    p.add_argument(
        "--include-all",
        action="store_true",
        help="Include non-playable channels too (default: playable-only)",
    )
    p.add_argument(
        "--m3u-include-alt-attrs",
        action="store_true",
        default=True,
        help="Include x-manifest-url and x-fallback-url attributes in EXTINF",
    )
    p.add_argument(
        "--group-title",
        default="DirecTV Stream",
        help="M3U group-title value",
    )
    p.add_argument(
        "--chno-start",
        type=int,
        default=0,
        help="Override channel numbers starting at this value (0 = use original numbers)",
    )

    args = p.parse_args(argv)

    # Resolve: named flags override positional
    args.allchannels_csv = args.allchannels or args.allchannels_csv_pos or ""
    args.playback_csv = args.playback or args.playback_csv or ""
    args.out_json = args.out_json_flag or args.out_json_pos or ""
    args.out_xml = args.out_xml_flag or args.out_xml_pos or ""
    args.out_m3u = args.out_m3u_flag or args.out_m3u_pos or ""

    # --url-mode is an alias for --m3u-url-mode
    if args.url_mode:
        args.m3u_url_mode = args.url_mode

    if not args.allchannels_csv:
        p.error("allchannels CSV is required (positional or --allchannels)")
    if not args.out_m3u:
        p.error("output M3U is required (positional or --out-m3u)")

    return args


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    args = _parse_args(argv)

    # Playback CSV is optional (may not exist yet in fresh installs)
    if args.playback_csv and os.path.exists(args.playback_csv):
        playback = _read_csv(args.playback_csv)
    else:
        playback = []

    allch = _read_csv(args.allchannels_csv)

    # Index AllChannels by ccid (for legacy joins) AND by resourceId (for canonical ids)
    all_by_ccid = {}
    all_by_rid = {}
    ccid_to_rid = {}

    for r in allch:
        ccid = _norm_ccid(
            r.get("ccid") or "",
            r.get("callSign") or r.get("callsign") or "",
            r.get("auth_url") or "",
        )
        rid = (r.get("resourceId") or r.get("resource_id") or "").strip()

        if ccid:
            all_by_ccid[ccid] = r
        if rid:
            all_by_rid[rid] = r
            if ccid:
                ccid_to_rid[ccid] = rid

    # Best playback row per ccid (prefer streamURL). We also preserve deeplink + playable if present.
    pb_by_ccid = {}
    for r in playback:
        token = (r.get("callsign_channel_token") or "").strip()
        ccid = _norm_ccid(r.get("ccid") or "", token, r.get("auth_url") or "")
        if not ccid:
            continue

        callsign_from_token = _callsign_from_token(token)

        stream_url = (r.get("streamURL") or r.get("stream_url") or "").strip()
        fallback_url = (r.get("fallbackStreamUrl") or r.get("fallback_stream_url") or "").strip()

        # If build_playback_map already computed these, prefer those.
        deeplink = (r.get("deeplink") or r.get("deepLink") or r.get("deep_link") or "").strip()
        playable = r.get("playable")
        playable_bool = _truthy(playable) if playable is not None and str(playable).strip() != "" else None

        cand = {
            "ccid": ccid,
            "callsign_token": token,
            "callsign_from_token": callsign_from_token,
            "streamURL": stream_url,
            "fallbackStreamUrl": fallback_url,
            "keyframeUrl": (r.get("keyframeUrl") or r.get("keyframe_url") or "").strip(),
            "auth_url": (r.get("auth_url") or "").strip(),
            "deeplink": deeplink,
            "playable": playable_bool,
        }

        cur = pb_by_ccid.get(ccid)
        if not cur:
            pb_by_ccid[ccid] = cand
        else:
            # prefer the row that has streamURL
            if (not (cur.get("streamURL") or "").strip()) and stream_url:
                pb_by_ccid[ccid] = cand

    # UNION of ccids: include channels even if playback is missing for them
    ccids = set(all_by_ccid.keys()) | set(pb_by_ccid.keys())

    merged = []
    for ccid in ccids:
        meta = all_by_ccid.get(ccid) or {}
        pb = pb_by_ccid.get(ccid) or {}

        rid = (meta.get("resourceId") or meta.get("resource_id") or "").strip() or ccid_to_rid.get(ccid, "")
        xmltv_id = f"dtv-{rid}" if rid else f"dtv-ccid-{ccid}"

        number = (meta.get("channelNumber") or meta.get("channel_number") or "").strip()

        callsign = (
            (meta.get("callSign") or meta.get("callsign") or "").strip()
            or (pb.get("callsign_from_token") or "").strip()
        )
        name = (meta.get("channelName") or meta.get("channel_name") or "").strip() or callsign or f"DTV {ccid}"
        logo = (meta.get("logoUrl") or meta.get("logo_url") or "").strip()
        if not logo:
            logo = _imageserver_chlogo(rid, w=60, h=45)

        manifest_url = (pb.get("streamURL") or "").strip()
        fallback_url = (pb.get("fallbackStreamUrl") or "").strip()
        keyframe = (pb.get("keyframeUrl") or "").strip()

        # If playback CSV includes deeplink already, use it; otherwise compute if we have rid + callsign.
        deeplink = (pb.get("deeplink") or "").strip()
        if not deeplink and rid and callsign:
            deeplink = f"dtvnow://deeplink.directvnow.com/play/channel/{callsign}/{rid}"

        playable_val = pb.get("playable")
        if playable_val is None:
            # best-effort heuristic if older playback_map.csv doesnâ€™t have 'playable'
            playable_val = bool(deeplink or manifest_url or fallback_url)
        else:
            playable_val = bool(playable_val)

        merged.append(
            {
                "xmltv_id": xmltv_id,
                "resourceId": rid,
                "ccid": ccid,
                "number": number,
                "callsign": callsign,
                "name": name,
                "logo": logo,
                "playable": playable_val,
                "deeplink": deeplink,
                "manifest_url": manifest_url,
                "fallback_url": fallback_url,
                "keyframe_url": keyframe,
            }
        )

    def sk(ch):
        n = (ch.get("number") or "").strip()
        return (int(n) if n.isdigit() else 999999, ch.get("name") or "", ch.get("ccid") or "")

    merged.sort(key=sk)

    # JSON (skip if --only-m3u or no output path)
    if args.out_json and not args.only_m3u:
        with open(args.out_json, "w", encoding="utf-8") as f:
            json.dump({"channels": merged}, f, indent=2)

    # XMLTV channels (channels-only) - skip if --only-m3u or no output path
    if args.out_xml and not args.only_m3u:
        tv = Element("tv")
        tv.set("source-info-name", "DirecTV Stream HAR-free")
        tv.set("generator-info-name", "build_channels_exports.py")

        for ch in merged:
            c = SubElement(tv, "channel")
            c.set("id", ch["xmltv_id"])

            dn = SubElement(c, "display-name")
            dn.text = f'{ch.get("number") or ""} {ch.get("name") or ""}'.strip()

            dn2 = SubElement(c, "display-name")
            dn2.text = ch.get("name") or ""

            if ch.get("logo"):
                icon = SubElement(c, "icon")
                icon.set("src", ch["logo"])

        with open(args.out_xml, "wb") as f:
            f.write(_pretty_xml_bytes(tv))

    # M3U
    include_all = bool(args.include_all)
    mode = args.m3u_url_mode
    group_title = args.group_title
    include_alt = bool(args.m3u_include_alt_attrs)
    chno_start = args.chno_start or 0
    chno_counter = chno_start

    with open(args.out_m3u, "w", encoding="utf-8", newline="") as f:
        f.write("#EXTM3U\n")
        for ch in merged:
            if (not include_all) and (not ch.get("playable")):
                continue

            url = _pick_m3u_url(ch, mode)
            if not (url or "").strip():
                continue

            tvg_id = ch["xmltv_id"]
            tvg_name = ch.get("name") or tvg_id
            tvg_logo = ch.get("logo") or ""

            # Channel number: sequential from chno_start, or original
            if chno_start > 0:
                tvg_chno = str(chno_counter)
                chno_counter += 1
            else:
                tvg_chno = ch.get("number") or ""

            display = (tvg_name or tvg_id).strip()

            attrs = [
                f'tvg-id="{_m3u_escape_attr(tvg_id)}"',
                f'tvg-name="{_m3u_escape_attr(tvg_name)}"',
                f'tvg-logo="{_m3u_escape_attr(tvg_logo)}"' if tvg_logo else "",
                f'tvg-chno="{_m3u_escape_attr(tvg_chno)}"' if tvg_chno else "",
                f'group-title="{_m3u_escape_attr(group_title)}"',
                f'x-ccid="{_m3u_escape_attr(ch.get("ccid") or "")}"',
                f'x-resource-id="{_m3u_escape_attr(ch.get("resourceId") or "")}"' if (ch.get("resourceId") or "") else "",
                f'x-callsign="{_m3u_escape_attr(ch.get("callsign") or "")}"' if (ch.get("callsign") or "") else "",
            ]

            if include_alt:
                if (ch.get("manifest_url") or "").strip():
                    attrs.append(f'x-manifest-url="{_m3u_escape_attr(ch.get("manifest_url") or "")}"')
                if (ch.get("fallback_url") or "").strip():
                    attrs.append(f'x-fallback-url="{_m3u_escape_attr(ch.get("fallback_url") or "")}"')

            attrs = [a for a in attrs if a]

            f.write(f'#EXTINF:-1 {" ".join(attrs)},{display}\n')
            f.write(f"{url}\n")

    with_stream = sum(1 for ch in merged if (_pick_m3u_url(ch, mode) or "").strip())
    playable_cnt = sum(1 for ch in merged if ch.get("playable"))
    if args.out_json and not args.only_m3u:
        print(f"Wrote: {args.out_json}")
    if args.out_xml and not args.only_m3u:
        print(f"Wrote: {args.out_xml}")
    print(f"Wrote: {args.out_m3u}")
    print(f"Playback rows: {len(playback)} | AllChannels rows: {len(allch)}")
    print(f"Channels total: {len(merged)} | playable: {playable_cnt} | with selected-url({mode}): {with_stream}")
    if not include_all:
        print("M3U filter: playable-only (use --include-all to include non-playable)")


if __name__ == "__main__":
    main()
