$ErrorActionPreference = "Continue"
Set-Location "C:\Users\gdp\Documents\Claude\Projects\🗺️ GHL Agency\publishing-dreamteam"

$msg = @"
Real chapter timestamps: replace dead timedtext with youtube-transcript-api

The old video.google.com/timedtext endpoint returns HTTP 200 with 0 bytes for
EVERY video, listed or unlisted (probed 2026-07-21). The handover's long-held
'empty for unlisted videos' diagnosis was wrong, so chapters were estimated at
130 wpm on 100% of packs, not just some. The modern youtube.com/api/timedtext
is also empty without a signature, so there is no cheap URL fix.

- requirements: youtube-transcript-api
- yt.fetch_transcript: rewritten onto youtube-transcript-api, English only,
  run in a thread so the SSE stream is not blocked. Never raises; failure logs
  the exception type (RequestBlocked/IpBlocked = datacenter IP flagged) and
  degrades to the previous estimated-chapter behaviour.
- yt.format_transcript_for_prompt: full-fidelity transcript, each line prefixed
  with its real [m:ss]. Deliberately not downsampled - input is cheap and a
  5-min video is only ~1.2k tokens.
- yt.snap_chapters_to_segments: chapter times are NEVER trusted to the model.
  An LLM invents plausible timestamps even with the real ones in the prompt, so
  it only chooses and titles a boundary; the second comes from the caption data.
  Also enforces YouTube's rules (first 0:00, ascending, >=10s apart) which
  silently disable chapters if broken.
- llm PACK_PROMPT: must copy a timestamp verbatim; 130 wpm is now the
  empty-transcript-only path.
- engine: stores transcript_segments + transcript_source always. Timing and
  text kept separate so paid-tier translation rewrites only the text and reuses
  the timing verbatim across languages.
- config: new flag srt_output, default OFF. Raw ASR SRT is not shipped free -
  the video already carries these same auto-captions, and uploading a copy
  strips YouTube's 'automatic' label off the errors. Cleaned SRT is paid.

Adversarial local test on j1mRs-YInKQ (invented, out-of-order, sub-10s,
malformed and past-the-end times): 117 segments, zero violations.
"@

git checkout dev 2>&1
git merge --ff-only main 2>&1
git add -A 2>&1
git commit -m $msg 2>&1
"--- push dev ---"
git push origin dev 2>&1
"--- ff-merge to main ---"
git checkout main 2>&1
git merge --ff-only dev 2>&1
"--- push main ---"
git push origin main 2>&1
"--- final ---"
git log --oneline -3
git status --short
