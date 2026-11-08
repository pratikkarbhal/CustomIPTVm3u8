Curated list, for personal use only 

# CustomIPTVm3u8 - Guide (EPG)
[![Update guide](https://github.com/pratikkarbhal/CustomIPTVm3u8/actions/workflows/update-guide2.yml/badge.svg?event=workflow_run)](https://github.com/pratikkarbhal/CustomIPTVm3u8/actions/workflows/update-guide2.yml)

Program guide for the channels in `playlist.m3u`. It updates itself daily.

## Guide URL

Paste this in the EPG / XMLTV field of your IPTV app:

```
https://raw.githubusercontent.com/YOUR-USERNAME/CustomIPTVm3u8/main/guide.xml
```

## How it works

1. GitHub Actions runs daily at 02:30 UTC (or manually from the Actions tab).
2. `make_epg_channels.py` reads the `tvg-id` of each channel in `playlist.m3u` and picks the matching channels from the iptv-org Airtel Xstream list.
3. The [iptv-org EPG tool](https://github.com/iptv-org/epg) downloads 3 days of schedules for those channels.
4. The result is saved as `guide.xml`. It is only replaced if it contains programmes.

## Files

| File | Purpose |
|------|---------|
| `playlist.m3u` | Your playlist (the guide channels are picked from this) |
| `make_epg_channels.py` | Matches your channels to the guide channel list |
| `.github/workflows/update-guide.yml` | Daily automation |
| `guide.xml` | Output, updated automatically |

## Notes

- Channels with no guide are listed as `NO GUIDE` in the Actions log.
- To read the playlist from another place, change `PLAYLIST` at the top of `make_epg_channels.py` to its full `https://` link.
- Setup: Settings -> Actions -> General -> Workflow permissions -> **Read and write permissions**.
- 
