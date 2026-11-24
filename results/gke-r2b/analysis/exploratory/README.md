# Exploratory three-tool contrasts

`tuned_vs.py` computes the exploratory three-tool contrasts of run 2b: the tuned three-tool Parseable configuration
against ClickHouse and against Grafana LGTM, as uncached USD per solved question, for each model. It uses the
registered analysis code (`otb.report.contrast` and `k8s/gke/r2_analysis.py`) and the registered bootstrap seed and
resample count from `results/gke-r2b/r2b-agent/run.yaml`. These contrasts are not in the pre-registration.

Run it from the repository root:

    uv run python results/gke-r2b/analysis/exploratory/tuned_vs.py > results/gke-r2b/analysis/exploratory/tuned_vs.txt

`tuned_vs.txt` is its output. Each line holds the contrast's name, the point estimate of the ratio, the lower and
upper bounds of its interval, the number of shared questions, and the p-value.

## Other numbers the paper states

`paper_numbers.py` prints two sets of numbers that the registered analysis does not, and `paper_numbers.txt` is its
output. Run it the same way.

- The three-tool configuration against ClickHouse and Grafana LGTM as billed USD per solved question, with the
  registered code, seed and resample count. Each line holds the contrast's name, the ratio and its interval.
- How much of a gap in input tokens per attempt the tool list explains. The tool list is taken as the difference
  in first-round input tokens. It is multiplied by the rounds per attempt of the configuration with the shorter
  list and divided by the gap. The Sonnet lines give the paper's "about 41%" (Grafana LGTM against the three-tool
  configuration) and the upper end of its "86% to 98%" (the 37-tool server against the three-tool configuration).
  The Opus line gives the lower end, 85.5%.
