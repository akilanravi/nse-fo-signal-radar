"""Create the nightly paper replay workbook without changing source results."""
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

import xlsxwriter
from xlsxwriter.utility import xl_col_to_name

NAMES = (
    "CPR Trend", "Price vs CPR", "Camarilla", "EMA 9-21", "VWAP", "RSI 14",
    "Supertrend", "Volume Spike", "NIFTY Tide", "Combined 7-of-9",
)
LABELS = {
    "underlying": "Underlying", "side": "Side", "status": "Status",
    "signal_ist": "Signal (IST)", "entry_premium": "Entry premium",
    "exit_premium": "Exit premium", "return_pct_gross": "Gross return",
    "gross_pnl_inr_one_lot": "Gross P&L (₹)", "reason": "Reason",
    "contract": "Option contract", "entry_ist": "Entry (IST)", "exit_ist": "Exit (IST)",
    "underlying_close_at_signal": "Underlying close", "strike": "Strike",
    "expiry": "Expiry", "lot_size": "Lot size", "security_id": "Security ID",
    "target_trigger": "Target trigger", "stop_trigger": "Stop trigger",
    "exit_signal_ist": "Exit signal (IST)", "trigger_candle_ist": "Trigger candle (IST)",
    "charges_inr": "Charges (₹)", "net_pnl_inr": "Net P&L (₹)",
    "session": "Session", "strategy": "Strategy", "price_source": "Price source",
}
FIRST = (
    "underlying", "side", "status", "signal_ist", "entry_premium", "exit_premium",
    "return_pct_gross", "gross_pnl_inr_one_lot", "reason", "contract", "entry_ist",
    "exit_ist",
)
WIDTHS = {
    "underlying": 17, "side": 9, "status": 16, "reason": 38, "contract": 36,
    "price_source": 47,
}
MONEY = {
    "entry_premium", "exit_premium", "gross_pnl_inr_one_lot",
    "underlying_close_at_signal", "strike", "target_trigger", "stop_trigger",
    "charges_inr", "net_pnl_inr",
}


def write_workbook(path, rows, fields, session_day):
    """Preserve all CSV columns and rows, including skipped and unresolved trades."""
    path = Path(path)
    rows_by = defaultdict(list)
    for row in rows:
        rows_by[row["strategy"]].append(row)
    codes = [f"C{i}" for i in range(1, 11)]
    unknown = rows_by.keys() - set(codes)
    if unknown:
        raise ValueError(f"Unexpected paper strategy codes: {sorted(unknown)}")
    columns = [k for k in FIRST if k in fields] + [k for k in fields if k not in FIRST]
    with xlsxwriter.Workbook(path, {"strings_to_formulas": False, "strings_to_urls": False}) as wb:
        title = wb.add_format({"bold": True, "font_size": 19, "font_color": "#172C48"})
        note = wb.add_format({"font_color": "#475569"})
        head = wb.add_format({"bold": True, "bg_color": "#233F62", "font_color": "#FFFFFF"})
        money = wb.add_format({"num_format": "#,##0.00;[Red](#,##0.00);0.00"})
        percent = wb.add_format({"num_format": "0.00%;[Red](0.00%);0.00%"})
        integer = wb.add_format({"num_format": "#,##0"})
        stamp = wb.add_format({"num_format": "hh:mm:ss"})
        short_date = wb.add_format({"num_format": "yyyy-mm-dd"})
        total = wb.add_format({"bold": True, "bg_color": "#E9F0F8", "num_format": "#,##0"})
        summary = wb.add_worksheet("Summary")
        summary.set_tab_color("#233F62")

        def banner(sheet, name, details):
            sheet.hide_gridlines(2)
            sheet.set_row(1, 30)
            sheet.set_row(2, 20)
            sheet.set_row(4, 24)
            sheet.merge_range("A2:H2", name, title)
            sheet.merge_range("A3:H3", details, note)
            sheet.freeze_panes(5, 0)

        label_day = session_day.strftime("%d %B %Y")
        banner(summary, f"Paper trades | {label_day}",
               "Ten independent tests. Gross P&L per listed lot. Charges and net P&L are not supplied.")
        summary.write_row(4, 0, ["Condition", "Rows", "Closed", "Skipped", "Unresolved",
                                 "CE gross (₹)", "PE gross (₹)", "Gross P&L (₹)"], head)
        summary.set_column("A:A", 25)
        summary.set_column("B:D", 12, integer)
        summary.set_column("E:E", 15, integer)
        summary.set_column("F:H", 19, money)
        for i, (code, name) in enumerate(zip(codes, NAMES)):
            tab_name = f"{code} {name}"
            sh = wb.add_worksheet(tab_name)
            data = rows_by[code]
            banner(sh, f"{tab_name} | {label_day}",
                   f"{len(data):,} rows. Filter Status to see only closed trades. Source: nightly replay CSV.")
            table_headers = [{"header": LABELS.get(k, k), "header_format": head} for k in columns]
            if data:
                sh.add_table(4, 0, 4+len(data), len(columns)-1,
                             {"name": f"Trades{code}", "columns": table_headers,
                              "style": "Table Style Medium 2"})
            else:
                sh.write_row(4, 0, [x["header"] for x in table_headers], head)
                sh.write(5, 0, "No rows for this condition.", note)
            for j, key in enumerate(columns):
                sh.set_column(j, j, WIDTHS.get(key, 18))
            for rr, row in enumerate(data, 5):
                for j, key in enumerate(columns):
                    val = row.get(key)
                    if val is None or val == "":
                        continue
                    if key.endswith("_ist"):
                        # Replay timestamps are already in IST. Preserve their wall time in Excel.
                        sh.write_datetime(rr,j,datetime.fromisoformat(str(val)).replace(tzinfo=None),stamp)
                    elif key in {"session","expiry"}:
                        sh.write_datetime(rr,j,date.fromisoformat(str(val)),short_date)
                    elif key == "return_pct_gross":
                        sh.write_number(rr,j,float(val)/100,percent)
                    elif key in MONEY or key == "lot_size":
                        sh.write_number(rr,j,float(val),integer if key == "lot_size" else money)
                    else:
                        sh.write_string(rr,j,str(val))
            sr = i+5
            er = sr+1
            summary.write(sr,0,tab_name)
            stats = Counter(r["status"] for r in data)
            pnl = {side:sum(float(r["gross_pnl_inr_one_lot"]) for r in data
                            if r["status"]=="CLOSED" and r["side"]==side and
                            r.get("gross_pnl_inr_one_lot") not in (None,""))
                   for side in ("CE","PE")}
            prefix = f"'{tab_name}'!"
            def ref(key):
                col=xl_col_to_name(columns.index(key))
                return f"{prefix}{col}6:{col}{max(6, 5+len(data))}"
            status, side, amt = (ref(k) for k in ("status","side","gross_pnl_inr_one_lot"))
            if not data:
                summary.write_row(sr,1,[0]*7)
                continue
            summary.write_formula(sr,1,f"=COUNTA({status})",integer,len(data))
            for j, state in enumerate(("CLOSED","SKIPPED","UNRESOLVED"),2):
                summary.write_formula(sr,j,f'=COUNTIF({status},"{state}")',integer,stats[state])
            for j, right in enumerate(("CE","PE"),5):
                summary.write_formula(sr,j,f'=SUMIFS({amt},{side},"{right}",{status},"CLOSED")',money,pnl[right])
            summary.write_formula(sr,7,f"=SUM(F{er}:G{er})",money,pnl["CE"]+pnl["PE"])
        summary.write(16,0,"All logged rows",total)
        all_stats = Counter(r["status"] for r in rows)
        for j, (letter, cached) in enumerate(zip("BCDE",[len(rows),all_stats["CLOSED"],all_stats["SKIPPED"],all_stats["UNRESOLVED"]]),1):
            summary.write_formula(16,j,f"=SUM({letter}6:{letter}15)",total,cached)
        summary.merge_range("A19:H19","The conditions are separate tests. Do not add their P&L as one portfolio.",note)
