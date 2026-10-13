// Holdings heatmap: one tile per symbol, size proportional to market value,
// colour by unrealised profit. Tiles flash green or red when the price ticks.

import type { Holding } from "./api";

const FLASH_MS = 600;

export class Heatmap {
  private host: HTMLElement;
  private tiles = new Map<string, HTMLElement>();

  constructor(host: HTMLElement) {
    this.host = host;
    host.classList.add("heatmap");
  }

  render(holdings: Holding[], priceDirections: Record<string, "up" | "down" | "same">): void {
    const symbols = new Set(holdings.map((h) => h.symbol));

    for (const [symbol, el] of this.tiles) {
      if (!symbols.has(symbol)) {
        el.remove();
        this.tiles.delete(symbol);
      }
    }

    if (holdings.length === 0) {
      this.host.dataset.empty = "true";
      return;
    }
    delete this.host.dataset.empty;

    const totalValue = holdings.reduce((s, h) => s + Math.abs(h.market_value), 0);

    for (const h of holdings) {
      const share = totalValue > 0 ? Math.abs(h.market_value) / totalValue : 1 / holdings.length;
      let tile = this.tiles.get(h.symbol);
      if (!tile) {
        tile = this.createTile(h.symbol);
        this.host.append(tile);
        this.tiles.set(h.symbol, tile);
      }
      tile.style.flexGrow = String(Math.max(0.05, share));
      tile.dataset.pnl = h.unrealized_pnl >= 0 ? "up" : "down";
      tile.dataset.side = h.quantity < 0 ? "short" : "long";
      tile.querySelector(".heatmap-ticker")!.textContent = tileLabel(h);
      tile.querySelector(".heatmap-value")!.textContent = formatMoney(Math.abs(h.market_value));
      renderExit(tile, h);

      const dir = priceDirections[h.symbol];
      if (dir === "up" || dir === "down") flash(tile, dir);
    }
  }

  private createTile(symbol: string): HTMLElement {
    const tile = document.createElement("div");
    tile.className = "heatmap-tile";
    tile.innerHTML = `
      <span class="heatmap-ticker">${symbol}</span>
      <span class="heatmap-value"></span>
    `;
    return tile;
  }
}

function tileLabel(holding: Holding): string {
  const crypto = holding.symbol.includes("/");
  const name = crypto ? holding.symbol.split("/")[0] : holding.symbol;
  if (crypto && holding.quantity < 0) return `${name} crypto short`;
  if (crypto) return `${name} crypto`;
  if (holding.quantity < 0) return `${name} SHORT`;
  return name;
}

function renderExit(tile: HTMLElement, holding: Holding): void {
  const label = formatExit(holding);
  let exit = tile.querySelector(".heatmap-exit");
  if (!label) {
    exit?.remove();
    return;
  }
  if (!exit) {
    exit = document.createElement("span");
    exit.className = "heatmap-exit";
    tile.append(exit);
  }
  exit.textContent = label;
}

function formatExit(holding: Holding): string {
  const bits: string[] = [];
  if (holding.stop_loss != null) bits.push(`stop ${holding.stop_loss.toFixed(2)}`);
  if (holding.take_profit != null) bits.push(`open past ${holding.take_profit.toFixed(2)}`);
  if (holding.trail_percent != null) bits.push(`trail ${holding.trail_percent}%`);
  return bits.join(" · ");
}

function flash(tile: HTMLElement, dir: "up" | "down"): void {
  tile.classList.remove("flash-up", "flash-down");
  // Force reflow so the animation restarts when the direction repeats.
  void tile.offsetWidth;
  tile.classList.add(dir === "up" ? "flash-up" : "flash-down");
  setTimeout(() => tile.classList.remove("flash-up", "flash-down"), FLASH_MS);
}

function formatMoney(n: number): string {
  if (n >= 1_000_000) return `$${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `$${(n / 1_000).toFixed(1)}k`;
  return `$${n.toFixed(0)}`;
}
