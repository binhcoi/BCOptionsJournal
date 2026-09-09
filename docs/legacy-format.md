# The legacy spreadsheet format

One row per option position, open and close on the same row, derived columns
by formula. This describes the format so the importer can be read without the
file. No real data appears here.

## Columns

Read positionally: the header uses `Quantity` twice.

| # | Column | Meaning |
| --- | --- | --- |
| 0 | `ID` | First 8 characters of `GUID`; display only |
| 1 | `Date` | Open date |
| 2 | `Option` | Composite label; ignored |
| 3 | `GUID` | Row identity; becomes the position's primary key |
| 4 | `Rolled GUID` | Predecessor this position rolled from |
| 5 | `Symbol` | Underlying |
| 6 | `Expiration` | Expiry |
| 7 | `Strike` | Strike |
| 8 | `Type` | `C` or `P` |
| 9 | `Quantity` | Signed: positive short, negative long |
| 10 | `Open_U` | Opening premium per share |
| 11 | `Buy Write` | Per-share price of shares bought with the option |
| 12 | `Fee` | Total opening fee |
| 13 | `Close Date` | Close date |
| 14 | `Close_U` | Close price; on an open row, the profit target |
| 15 | `Quantity` | Closing quantity, the negation of column 9 |
| 16 | `C_Fee` | Total closing fee |
| 17 | `Status` | `Open`, `Closed`, `Expired`, `Rolled`, `Assigned` |
| 18 | `P/L` | Derived |
| 19 | `Open` | Derived: opening cash flow |
| 20 | `Close` | Derived: closing cash flow |
| 21 | `Assignment` | Derived: share cash flow for the row |
| 22 | `E P/L` | Derived: expected P/L at the target, open rows only |
| 23 | `E Closing` | Derived: expected closing cash flow at the target |
| 24 | `Cost basis` | Derived: roll-chain P/L carried forward |
| 25 | `Put Risk` | Derived: cash obligation on a put; `-` on a call |
| 26 | `Rolled ID` | First 8 characters of `Rolled GUID` |

## Derived formulas

`Q` is the signed quantity, `mult` the multiplier (100).

```
open_cash  =  Q × mult × Open_U  − Fee
close_cash = −Q × mult × Close_U − C_Fee
P/L        = open_cash + close_cash          (zero while open)
```

`Cost basis` is chain carry: the predecessor's `P/L` plus its own `Cost
basis`, following `Rolled GUID`.

`E P/L` and `E Closing` are values at a profit target (50% by default), open
rows only. `Close_U` there is the target price, not a market mark:

```
net    = open_cash + carry
price  = −(net × pct − net + C_Fee) / (Q × mult)      clamped at 0
```

`Assignment` is share cash flow: negative on acquisition, positive on
disposal, the net of both when a buy-write is called away. Derivable from
`Buy Write` and the strike, so the importer recomputes it and diffs it.

## Cell conventions

- `(1,234.00)` is −1234.00
- `"$16,500.00"`, `-$2,000.00`
- `-` means not applicable
- `0` and `0.00` are zero, distinct from blank

## Quirks

The importer flags these; it does not fix them.

- **Only options get a row.** Outright share trades are disguised as options. Tells: a strike off any listed increment, zero premium, expiry at or before open, P/L equal to negated fees. Two or more together mean a share transaction. A consequence: a ticker's share count can go negative, and the importer blocks that ticker rather than compute through it.
- **Partial events are flattened.** Where part of a position was assigned and the rest rolled, rows were reshaped by hand so every close negates its open. Fingerprint: quantity drops across a roll link, often with a fee that does not match the contract count.
- **Placeholder rows** park a defunct position as a zero-strike, zero-premium contract with a far-future expiry.
- **Column reuse.** `E P/L` and `E Closing` are zero except on open rows; `Cost basis` carries the chain total on every row.

## What the importer does

Writes what the sheet says, changes nothing. Recomputes eight derived figures
per row and diffs them against the sheet's columns: exact, within rounding, or
a disagreement. Suspicious rows are flagged, never corrected. Five years of
hand-kept accounting is the acceptance test.
