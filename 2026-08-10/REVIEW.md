# Fond app-only reel - 2026-08-10 (review, do NOT publish)

## Hosted deliverables (gofile; retain well beyond the 72h scheduling window)
- MASTER (video/mp4): https://gofile.io/d/RwmGE8  |  8,784,965 bytes  |  md5 9492cec964a4cf9cf7bc1db12f1e6bec
- COVER (1080x1920 jpg): https://gofile.io/d/EzFfbI
- CONTACT SHEET full-res (jpg): https://gofile.io/d/u73eJZ

## Specs
1080x1920 - 30 fps - 16.633 s - -14.6 LUFS - 8.78 MB - H.264 (Constrained Baseline) + AAC 44.1k stereo

## Voiceover
Gemini Kore (warm, unhurried, one-close-friend tone). 37 words, 15.23 s speech, ~2.30 wps over the 16.63 s runtime (locked 4-sentence copy + <=17 s gate make <=2.0 wps unattainable; Kore beats trimmed to <=0.85 s).

## Segment beats (start-end, seconds)
- SEG1 HOOK: 0.000-2.533 ("Your camera roll / is not a / memory.", italic terracotta accent + underline wipe, push-in 1.00->1.06)
- SEG2 RECOGNITION: 2.533-8.833 (Pexels 3x3 camera-roll grid, parallax; "14,000 photos"; "A photo keeps her face. / Never her voice.")
- SEG3 RECORD: 8.833-11.900 (Fond record mock, pulsing terracotta button, VO-synced showwaves waveform, super "a voice journal you just talk to", caption)
- SEG4 ASK: 11.900-14.600 (query chip -> answer card slide-up in Newsreader italic terracotta; caption; SEND-TRIGGER lower-third with drawn arrow in final ~1.3 s)
- SEG5 END CARD: 14.600-16.633 (ivory; Fond wordmark; kicker; 16-bar waveform; "keep what you're fond of."; "Your first month is free."; "App Store - Google Play")

## QA (all PASS)
- runtime 16.633 s <= 17.0 gate; dims 1080x1920; fps 30; LUFS -14.6
- chip ink coverage 79-87% (>3%); hook/recognition/end-card median luma 245-248 (bright)
- key text within safe margins (SEG1 side/top columns ~247 = clean paper)
- send-trigger arrow renders as a drawn terracotta glyph (2197 is tofu in Hanken)
- SEG2 numeral 21% terracotta coverage

## Notes / blockers
- litterbox 72h returned HTTP 500 (BunkerWeb WAF) on every retry; 0x0.st returned empty; both blocked from the Composio build sandbox. Durable gofile links used instead.
- Binary JPG push to this repo via the Composio GitHub tool hit its base64 double-encode/truncation behavior, so the reviewable contact sheet lives at the gofile link above; this text manifest is the reliable GitHub review bridge.
