# The legacy spreadsheet format

The importer reads a specific hand-maintained spreadsheet: one row per option
position, opened and closed, with derived columns computed by formula. This
describes the format and its quirks so the import code can be understood
without the original file.

No real trade data appears here or anywhere else in this repository.

## Columns

Read **positionally**, because the header repeats the name `Quantity` for both
the opening and closing legs.

| # | Column | Meaning |
| --- | --- | --- |
| 0 | `ID` | First 8 characters of `GUID`; display only |
| 1 | `Date` | Open date |
| 2 | `Option` | Composite label; redundant, ignored |
| 3 | `GUID` | Row identity — becomes the position's primary key |
| 4 | `Rolled GUID` | The predecessor this position rolled from |
| 5 | `Symbol` | Underlying |
| 6 | `Expiration` | Expiry |
| 7 | `Strike` | Strike |
| 8 | `Type` | `C` or `P` |
| 9 | `Quantity` | **Signed: positive short, negative long** |
| 10 | `Open_U` | Premium per share on opening |
| 11 | `Buy Write` | Per-share price of shares bought alongside the option |
| 12 | `Fee` | Total opening fee |
| 13 | `Close Date` | Close date |
| 14 | `Close_U` | Close price — or, on an open row, a **profit target** |
| 15 | `Quantity` | Closing quantity; the negation of column 9 |
| 16 | `C_Fee` | Total closing fee |
| 17 | `Status` | `Open`, `Closed`, `Expired`, `Rolled`, `Assigned` |
| 18 | `P/L` | Derived |
| 19 | `Open` | Derived: opening cash flow |
| 20 | `Close` | Derived: closing cash flow |
| 21 | `Assignment` | Derived: share cash flow for the row |
| 22 | `E P/L` | Derived: **expected** P/L at the profit target, open rows only |
| 23 | `E Closing` | Derived: expected closing cash flow at that target |
| 24 | `Cost basis` | Derived: roll-chain P/L carried forward |
| 25 | `Put Risk` | Derived: cash obligation on a put; `-` on a call |
| 26 | `Rolled ID` | First 8 characters of `Rolled GUID` |

## Derived formulas

With `Q` as the signed quantity and `mult` the contract multiplier (100):

```
open_cash  =  Q × mult × Open_U  − Fee
close_cash = −Q × mult × Close_U − C_Fee
P/L        = open_cash + close_cash          (zero while open)
```

One formula covers long and short, calls and puts.

**`Cost basis`** is the chain carry: the predecessor's `P/L` plus the
predecessor's own `Cost basis`, following `Rolled GUID`. On an assignment row
the same figure feeds the effective share basis, which is where the column name
comes from.

**`E P/L` / `E Closing`** are *expected* values at a profit target (50% by
default), populated on open rows only. `Close_U` there is a target price, not a
market mark — nothing in the sheet quotes the market. The target is the close
price at which the chain nets the target fraction of its available credit:

```
net    = open_cash + carry
price  = −(net × pct − net + C_Fee) / (Q × mult)
```

Clamped at zero: a chain already underwater has no profit target, and its best
case is expiring worthless.

**`Assignment`** is share cash flow attributable to the row — negative on
acquisition, positive on disposal, and the *net* of both when a buy-write is
called away. It is fully derivable from `Buy Write` and the assigned strike, so
the importer recomputes it and diffs it rather than trusting it.

## Cell conventions

- Accounting negatives: `(1,234.00)` means −1234.00
- Currency and separators: `"$16,500.00"`, `-$2,000.00`
- A bare `-` means not applicable (e.g. `Put Risk` on a call)
- `0` and `0.00` mean zero, which is *not* the same as blank

## Known quirks

These are properties of the format, and the importer flags rather than fixes
them.

**Only option trades get a row.** There is nowhere to record buying or selling
stock on its own, so such transactions get disguised as options. The tell-tales:
a strike that isn't on a listed increment, zero premium, an expiry at or before
the open date, or a P/L equal to the negated fees. Two or more together mean the
row is standing in for a share transaction.

A direct consequence: a ticker's share count can go **negative**, because
disposals were recorded while the matching purchase had nowhere to live. A
negative count is impossible, and any per-share figure derived from it is
meaningless — so the importer treats it as blocking rather than computing
through it.

**Partial events get flattened.** Where part of a position was assigned and the
rest rolled, rows were reshaped by hand to preserve one row per position. The
history therefore *looks* as though every close exactly negates its open. The
fingerprint of a reshaped row is a quantity that drops across a roll link, often
together with a fee inconsistent with the contract count.

**Placeholder rows exist**, parking a defunct position as a zero-strike, zero-
premium contract with a far-future expiry.

**Column reuse.** `E P/L` and `E Closing` are meaningful only on open rows and
sit at zero elsewhere; `Cost basis` carries the chain total on every row type.

## What the importer does with all this

Reads faithfully — it writes what the sheet says and changes nothing. It then
recomputes eight derived figures per row and diffs them against the sheet's own
columns, classifying each as exact, within a cent or two of rounding, or a real
disagreement. Suspicious rows are flagged for a human, never auto-corrected.

Five years of hand-maintained accounting is a far better oracle for a P/L engine
than any fixture, so the diff is the acceptance test.
