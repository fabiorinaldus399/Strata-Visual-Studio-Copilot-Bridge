"""Do Visual Studio's Copilot agent turns actually get released?

Visual Studio 2026 drives its Copilot agent through a child `copilot.exe` and an
internal event channel.  When a turn finishes the channel closes a few
milliseconds after `session.idle` arrives.  On some 18.10 builds the channel
never closes: the answer is rendered, but the Stop button stays active and the
chat log fills with "CLI event delivery fence was not observed".

This script reads Visual Studio's own chat logs and reports, per session, how
many turns released and how many did not, so you can tell a provider-side
problem apart from the Visual Studio one.

Usage:
    python vs_turn_verdict.py                 # every chat log in %TEMP%
    python vs_turn_verdict.py <vs-chat.log>   # specific log(s)
"""
import glob
import os
import re
import sys
from datetime import datetime, timedelta

TS = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})")

# VS logs each event twice: once as "Event <Name>" and once as
# "[Unhandled event type] <Name>".  Only count the first form.
IDLE = "Event AssistantIdleEvent"
CLOSED = "Event channel completed - exiting event loop"
FENCE = "fence was not observed"
TURN_END = "Event AssistantTurnEndEvent"
VERSION = re.compile(r"Copilot chat version (\S+) .*VS: (\S+)")


def default_logs():
    root = os.path.join(os.environ.get("TEMP", ""), "VSGitHubCopilotLogs")
    return sorted(glob.glob(os.path.join(root, "*.chat.log")))


def analyse(path):
    version = None
    turn_ends, idles, closes, fences = [], [], [], []
    for line in open(path, encoding="utf-8", errors="replace"):
        m = TS.match(line)
        if not m:
            continue
        t = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S.%f")
        if IDLE in line:
            idles.append(t)
        elif CLOSED in line:
            closes.append(t)
        elif FENCE in line:
            fences.append(t)
        elif TURN_END in line:
            turn_ends.append(t)
        elif version is None:
            v = VERSION.search(line)
            if v:
                version = f"chat {v.group(1)} / VS {v.group(2)}"

    name = os.path.basename(path).replace("_VSGitHubCopilot.chat.log", "")
    print(f"\n### {name}")
    if version:
        print(f"    {version}")
    if not idles:
        print("    no completed turns in this log")
        return 0, 0

    stuck = 0
    for i, t in enumerate(idles, 1):
        after = [c for c in closes if c >= t]
        near = [f for f in fences if t <= f <= t + timedelta(seconds=15)]
        if after and (after[0] - t).total_seconds() < 15:
            state = f"released  (channel closed {(after[0] - t).total_seconds() * 1000:.0f} ms later)"
        elif near:
            state = "STUCK     (fence time-out, channel never closed)"
            stuck += 1
        else:
            state = "STUCK     (channel never closed)"
            stuck += 1
        print(f"    turn {i}: {t.time()}  {state}")

    print(f"    model turns: {len(turn_ends)}   prompts: {len(idles)}   "
          f"released: {len(idles) - stuck}   stuck: {stuck}   "
          f"fence time-outs: {len(fences)}")
    return len(idles), stuck


def main(argv):
    logs = argv[1:] or default_logs()
    if not logs:
        print("no Visual Studio chat logs found")
        return 1
    prompts = stuck = 0
    for path in logs:
        p, s = analyse(path)
        prompts += p
        stuck += s
    print(f"\nverdict: {prompts - stuck}/{prompts} turns released.")
    if stuck:
        print("  session.idle reached Visual Studio in every stalled turn, so the provider")
        print("  side finished normally; the completion coordinator in this VS build did not.")
        print("  Update Visual Studio (Copilot chat 18.11.1011 or later).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
