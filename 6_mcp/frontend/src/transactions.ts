// A trader's recent trades, newest first. Mirrors the activity log's compact style.

import type { Transaction } from "./api";

const MAX_ROWS = 12;

export class TransactionsView {
  private host: HTMLElement;

  constructor(host: HTMLElement) {
    this.host = host;
    host.classList.add("txns");
  }

  render(transactions: Transaction[]): void {
    this.host.innerHTML = "";
    if (transactions.length === 0) {
      const empty = document.createElement("div");
      empty.className = "txn-empty";
      empty.textContent = "No trades yet";
      this.host.append(empty);
      return;
    }
    for (const t of transactions.slice(-MAX_ROWS).reverse()) {
      const row = document.createElement("div");
      row.className = "txn-row";

      const date = document.createElement("span");
      date.className = "txn-date";
      date.textContent = dateOf(t.timestamp);

      const side = document.createElement("span");
      side.className = "txn-side";
      const kind = tradeKind(t);
      side.dataset.side = kind;
      side.textContent = kind.toUpperCase();

      const detail = document.createElement("span");
      detail.className = "txn-detail";
      detail.textContent = `${t.symbol} ${formatQty(t.quantity)} @ ${formatPx(t.price)}`;

      row.append(date, side, detail);
      this.host.append(row);
    }
  }
}

function tradeKind(t: Transaction): "buy" | "sell" | "short" | "cover" {
  if (t.side === "short" || t.side === "cover") return t.side;
  return t.quantity >= 0 ? "buy" : "sell";
}

function formatQty(quantity: number): string {
  const abs = Math.abs(quantity);
  if (Number.isInteger(abs)) return String(abs);
  return abs.toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
}

function formatPx(price: number): string {
  if (price >= 100) return `$${price.toFixed(2)}`;
  if (price >= 1) return `$${price.toFixed(3)}`;
  return `$${price.toFixed(4)}`;
}

function dateOf(stamp: string): string {
  // "YYYY-MM-DD HH:MM:SS" -> "MM-DD"
  const parts = stamp.split(" ")[0].split("-");
  return parts.length === 3 ? `${parts[1]}-${parts[2]}` : stamp;
}
