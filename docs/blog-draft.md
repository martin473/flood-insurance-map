# Will it flood, and can I insure it? Building a flood map with an AI agent

*Draft. Numbers in [brackets] get filled in after the labels are checked and the final eval runs.*

I wanted a map for Rhode Island homeowners: projected flooding on top of the flood insurance maps. I built it with an AI coding agent over a day and a half. This post is about the decisions, because the agent wrote most of the code and the interesting part was deciding what to keep.

## The first version answered the wrong question

The first map had everything: FEMA zones, URI's storm model, NOAA high tides, URI's own flood zones, dotted outlines, dashed outlines, a legend with six entries. It was accurate and I couldn't use it. I told the agent it felt like getting the runaround, because what a homeowner wants to know is two things: will it flood here, and is it covered?

We cut it down to a year slider, one flooding layer and one insurance layer, and a card that answers in big type: "Yes: about 2.4 ft of water in a major storm by 2050." Then I realized flood insurance isn't the whole story for a buyer, so a third answer went in for homeowners insurance. The data didn't change. The question did.

## Insurers don't publish coverage maps

The next question was which insurers would actually cover a given house. I asked the agent to get each insurer's coverage map. There aren't any. Private flood insurers decide house by house, and their rules live in underwriting guides, brochures and broker comparison sheets, as PDFs.

Next I wanted a tap on the map to check each insurer live. The agent pushed back: quote forms need the homeowner's personal details, and insurers' terms don't allow automated submissions. That was the right call, so the project became: read the PDFs, extract the rules, check them, and apply them to a point on the map.

## Extraction you can check

`floodrules` is a Python and FastAPI service. A model reads a PDF and fills in a typed schema: which states, which flood zones, what's excluded (coastal barrier areas, floodways, homes over water), dollar limits.

The design choice I care most about: every rule has to come with a short quote from the document and a page number, and code checks that the quote is really there. If it isn't, the rule is dropped. The model's output is a claim, and the quote is the evidence.

## The eval is where the real work happened

I labeled what should come out of each document by hand, and the eval scores the model against it: precision (how much of what it found is right) and recall (how much of what's there it found).

The first real run, with Nemotron 3 Nano on Ollama Cloud, scored **10% recall**. Every fix after that came from reading the eval's list of misses:

1. **The model ran out of tokens before answering.** It's a reasoning model, and its hidden thinking used up the output budget before it wrote any JSON. Fix: turn thinking off for extraction.
2. **One bad value sank a whole document.** Ollama Cloud didn't hold the model to the JSON schema, so it wrote zone "AE" where the schema says "A", or "Building" instead of "dwelling". Fix: map known variants, then validate rule by rule and drop only what still doesn't fit, with the drops listed in the report.
3. **It extracted a commercial program.** The prompt said residential only. The model did it anyway, so the filter moved into code.

That got verified recall to **90%** and precision to **79%**.

## Two bugs the evals caught that I wouldn't have

**The model did math.** A brochure says loss of use is 20% of the dwelling limit. The model wrote "$450,000," which is 20% of $2.25M. The rule passed the quote check because "20% of dwelling" really is in the document. The fix was a second check: a dollar limit only counts if the amount appears in its quote.

**The same run doesn't give the same score.** The same model, prompt and documents scored 90% recall on my laptop and 81% in CI, even at temperature 0. A single eval number from a cloud model can be off by about 10 points, so the eval now repeats each run and reports a mean and range: **[X]% precision and [Y]% recall, mean of 3 runs ([low]–[high])**.

## A data gap that would have told someone the wrong thing

When the agent finally tested the map against the live data services, it found that URI's storm model has holes: some beachfront cells have no value at all. The map would have told someone with an oceanfront lot "No, it won't flood." The fix samples a small area around the tap instead of one point, and falls back to FEMA's coastal zone where URI has nothing. That test happened late, after I asked the agent to make sure the map actually answered the question. It should have happened first.

## What I'd point to in the agent's work

- **What I kept:** the quote check, the item-by-item repair, testing against recorded responses so CI never calls a model, and the eval report that lists every miss by name.
- **What I rejected or redirected:** a six-layer map that answered nothing; zip files as a delivery mechanism (we moved to a GitHub repo with CI); and the claim that "almost every property can get insurance." I asked about Florida, where insurers have been leaving, and the honest answer had two caveats the agent hadn't mentioned: the federal program's authorization expires December 11, 2026, and "available" isn't "affordable." 
- **What I had to check myself:** the labels. The agent wrote the first labels from summaries of the PDFs, and said so in every label file. Checking them against the documents moved precision from [before] to [after], because some of the model's "extra" findings were right and my labels were wrong.

## How it runs

- **CI on every push:** lint, type checks, [N] tests and a coverage gate, on two Python versions, plus a check that the Docker image starts and answers health checks. That container check caught a real bug: the app logged to stderr, where the container platform wasn't collecting it.
- **The eval runs on demand** as a GitHub Actions workflow with the model key as a secret. It posts the results table on the run page and saves the raw model answers, so I can rescore them locally for free after fixing a label.
- **Deployment:** the map and API run as one DigitalOcean app. The map calls `/api/property`, which does the lookups with caching, retries and per-source error tracking.

The code is at github.com/martin473/flood-insurance-map, and the map is live at [URL].
