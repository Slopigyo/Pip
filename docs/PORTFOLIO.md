# Showing Pip in a portfolio

The useful story is a product experiment with inspectable engineering decisions—not a claim that a chatbot can choose someone's career. A short synthetic walkthrough can show the work without requiring a reviewer to install a model or sharing a real life history.

## A concise project description

> Pip is a human-directed, AI-assisted local-first prototype for exploring how work, place, and everyday life fit together. It combines conversational input, reviewed résumé excerpts, and optional local dictation with a structured Person Map and portable exploration briefs. Its engineering focus is keeping model suggestions separate from user-approved state, preserving evidence and uncertainty, and minimizing external disclosure.

Describe the work you can explain: product direction, decisions and tradeoffs, hands-on evaluation, and the implementation you understand. Be open about AI assistance. Do not claim to have trained the underlying models, built live national search coverage, or validated user outcomes at scale.

## Use a separate synthetic workspace

Try the synthetic flow with your chosen model before recording. Automated checks use synthetic provider output, not a guarantee of live-model quality; see [validation status](PUBLISHING.md#validation-status). Show actual output and limitations, not fabricated screenshots presented as working behavior.

From PowerShell after the normal installation:

```powershell
.venv\Scripts\python.exe -m jobwolf --data-dir private\portfolio-demo --port 8766 --no-browser
```

This uses a different database from the normal `%LOCALAPPDATA%\JobWolfBench` workspace. The `private/` folder is ignored. Open the printed link in a clean Chrome or Firefox profile; do not show the terminal's token or the browser address bar in a recording. If reusing this demo directory, inspect it first and ensure it contains only synthetic material. Do not import your real workspace to populate the demo.

The app still uses your separately installed Ollama model; select an appropriate local model yourself. Speech remains optional and uses its separately installed assets. Neither is required merely to read the project on GitHub.

### Invented résumé text

Paste this into **Add a résumé** and label the source **Synthetic demo résumé**. This is invented material, not a real person's record:

```text
Taylor Example — fictional demonstration persona

2021–2025: Repair assistant, fictional community workshop
Troubleshot small appliances and documented repairs.
Helped visitors understand tool use and basic safety.

2018–2021: Stockroom assistant, fictional local retailer
Tracked parts and supplies and improved shelf labeling.

Completed a short introductory electronics course.
No professional license or degree is claimed in this example.
```

A synthetic opening message can add what a résumé does not capture:

> I enjoy figuring out why things stopped working and explaining a repair to someone. I would rather have a predictable daytime schedule than chase a prestigious title. I'm curious about hands-on technical work, but I'm not sure I want to do repairs all day. Help me explore that without assuming I need to move.

## A short walkthrough

Aim for two or three minutes, with slower inference waits shortened and disclosed if the video is edited:

1. **Begin with conversation.** Show a streaming reply and an editable next draft. Explain that chatting alone does not update the Person Map.
2. **Import the invented résumé.** Inspect the preview, correct something if needed, and save reviewed text. Show the numbered excerpts rather than a whole-document dump into every prompt.
3. **Choose context deliberately.** Select a few useful messages/excerpts and use **Find notes for my Person Map**. Show one proposed note, its source, and uncertainty; accept or correct it explicitly.
4. **Explore a possibility.** Select appropriate reviewed notes, request discovery, and discuss a tradeoff and an unknown. Do not imply the app researched a live vacancy or current pay.
5. **Export a useful artifact.** Request/review a brief and show the Markdown exploration plan. Label both the persona and any model-generated output clearly.
6. **Optional dictation clip.** Speak only invented content, show the editable transcript, and send it deliberately. It demonstrates local input, not general speech accuracy.

If the model refuses, misquotes evidence, or produces weak suggestions, show honest recovery or choose a smaller example. Do not bypass validation for a polished video.

## What to publish

A strong small portfolio presentation needs only:

- The README and a link to the source repository.
- Two or three cropped screenshots: conversation, explicit evidence review, and a synthetic brief.
- An optional short video, plus a caption that says **work in progress**, **synthetic example**, and which parts are model-generated.
- A short note on one design tradeoff: DPAPI is straightforward but Windows-specific; manual provider handoff preserves choice but is not one-click login; conservative context guards improve predictability but leave some model capacity unused.

A public live deployment is unnecessary for this portfolio and is not supported by the current local security boundary. Capture new media from synthetic data rather than blurring personal material after the fact. Check browser tabs, tooltips, filenames, startup links, desktop notifications, audio, and image metadata before sharing. Keep recordings and drafts outside Git until you have reviewed them.

Use [the publishing checklist](PUBLISHING.md) for licensing, Git history, repository settings, and the final decision to make the code public.
