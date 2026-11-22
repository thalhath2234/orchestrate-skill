# Reviewer effort sweep: opus/medium vs opus/high at 690be6e1eb8421f4c40dd8c48aa797da6cd4660e
Rule, written before any result:
- Switch the default to high only if high passes at least 2 more of the 12 defect trials
  AND no fewer of the 6 false-positive trials. Otherwise keep medium.
- A one-trial difference is noise.
- Trials lost to API or usage-limit errors are excluded and re-run, never counted as a miss.
Order: medium, high, high, medium. One trial per case per block.
