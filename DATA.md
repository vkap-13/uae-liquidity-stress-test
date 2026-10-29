# Data source and how to rebuild the input

**Source:** Central Bank of the UAE (CBUAE), *Monthly Statistical Bulletin – Banking & Monetary Statistics*,
Table 1 "Selected Monetary and Banking Indicators", row **"Deposits"** (footnote: excluding inter-bank
deposits), in **AED million**. Bulletins are published on the CBUAE Research and Statistics pages
(centralbank.ae). Accessed: 28 October 2026.

**Frequency:** quarter-end values, December 2020 to December 2025 (21 observations, 20 quarterly changes).
Table 1 shows quarter-end columns for most of the period, so a quarterly series is the most complete
consistent run available from it.

**Bulletins used** (latest published vintage taken where a figure was revised):

| Quarters | Bulletin |
|---|---|
| Dec 2020 – Sep 2022 | December 2022 |
| Mar 2023 – Sep 2023 | December 2024 |
| Dec 2021, Dec 2022, Dec 2023 – Dec 2025 | January 2026 |

**Checks applied:** every value was cross-checked against the same table's Residents and Non-Residents rows
(they sum to Deposits exactly in 19 quarters and within AED 1 million rounding in 2); values appearing in
more than one bulletin agree except for two preliminary-to-final revisions (Dec 2022, Dec 2024), where the
later figure is used. One bulletin prints Dec 2020 as "11,884,545"; this is the footnote marker fused to
1,884,545 and the correct value is confirmed by the component rows.

**Not to be confused with** "Cash Deposits at the Central Bank of the UAE" in the *Monthly Banking
Operations Statistics*. That is a monthly flow of banknotes lodged by banks, not the stock of customer
deposits, and is far more volatile.

The raw series is not included in this repository. To reproduce:

1. Read the Deposits row for each quarter from the bulletins above.
2. Create `data/cbuae_deposits_quarterly.csv`:

   ```
   Date,Deposits
   2020-12-31,1884545
   2021-03-31,...
   ```
3. Check: `python src/liquidity_stress.py --check data/cbuae_deposits_quarterly.csv --frequency quarterly`
4. Run:   `python src/liquidity_stress.py --csv data/cbuae_deposits_quarterly.csv --frequency quarterly`

Attribution: data © Central Bank of the UAE.
