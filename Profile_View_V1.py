import pandas as pd
import numpy as np
import sys
from datetime import timedelta
import argparse
from collections import defaultdict

# Optional: GUI support (matplotlib)
try:
    import matplotlib.pyplot as plt
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False


def get_key_levels(tpo_count_dict: dict, value_area_pct: float = 0.68):
    """Calculate POC, VAH, VAL from a count dictionary (works for both numeric and letters mode)"""
    if not tpo_count_dict:
        raise ValueError("No TPO data")
    
    poc_price = max(tpo_count_dict, key=tpo_count_dict.get)
    total_tpo = sum(tpo_count_dict.values())
    prices_sorted = sorted(tpo_count_dict.keys())
    poc_idx = prices_sorted.index(poc_price)

    left = poc_idx
    right = poc_idx
    value_tpo_count = tpo_count_dict[poc_price]
    target_tpo = total_tpo * value_area_pct

    while value_tpo_count < target_tpo and (left > 0 or right < len(prices_sorted) - 1):
        left_candidate = tpo_count_dict[prices_sorted[left - 1]] if left > 0 else -1
        right_candidate = tpo_count_dict[prices_sorted[right + 1]] if right < len(prices_sorted) - 1 else -1

        if left_candidate >= right_candidate and left > 0:
            left -= 1
            value_tpo_count += left_candidate
        elif right < len(prices_sorted) - 1:
            right += 1
            value_tpo_count += right_candidate
        else:
            break

    return {
        'poc': poc_price,
        'vah': prices_sorted[right],
        'val': prices_sorted[left],
        'total_tpo': total_tpo,
        'value_area_pct': value_area_pct,
        'prices_sorted': prices_sorted
    }


def compute_market_profile(df: pd.DataFrame, tick_size: float = 0.5,
                           period_minutes: int = 30, value_area_pct: float = 0.68,
                           use_letters: bool = False):
    """Build TPO profile (numeric count OR classic letters A/B/C...)"""
    df = df.copy()
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df = df.sort_values('timestamp').reset_index(drop=True)

    # Generate time brackets
    start_time = df['timestamp'].iloc[0].floor('min').replace(second=0, microsecond=0)
    end_time = df['timestamp'].iloc[-1] + timedelta(minutes=1)

    brackets = []
    current = start_time
    while current < end_time:
        bracket_end = current + timedelta(minutes=period_minutes)
        brackets.append((current, bracket_end))
        current = bracket_end

    # TPO data
    tpo = defaultdict(list) if use_letters else {}
    count_dict = {}  # always needed for POC / Value Area calculation

    for i, (start, end) in enumerate(brackets):
        letter = chr(ord('A') + i) if i < 26 else f"{chr(ord('A') + (i//26))}{chr(ord('A') + (i % 26))}"

        mask = (df['timestamp'] >= start) & (df['timestamp'] < end)
        if not mask.any():
            continue

        bracket_df = df[mask]
        low = bracket_df['low'].min()
        high = bracket_df['high'].max()

        p_start = np.ceil(low / tick_size) * tick_size
        p_end = np.floor(high / tick_size) * tick_size
        if p_start > p_end:
            continue

        prices = np.arange(p_start, p_end + tick_size, tick_size)
        for p in prices:
            p_clean = round(p / tick_size) * tick_size
            if use_letters:
                tpo[p_clean].append(letter)
            else:
                tpo[p_clean] = tpo.get(p_clean, 0) + 1

    # Build count_dict for key levels
    if use_letters:
        count_dict = {p: len(lst) for p, lst in tpo.items()}
    else:
        count_dict = tpo

    result = get_key_levels(count_dict, value_area_pct)
    result['tpo_dict'] = tpo
    result['use_letters'] = use_letters
    result['period_minutes'] = period_minutes
    return result


def display_profile(result: dict, bin_size: int = 10, title: str = "Market Profile"):
    """Print ASCII profile - supports both # bars and classic A/B/C letters"""
    tpo_dict = result['tpo_dict']
    poc = result['poc']
    vah = result['vah']
    val = result['val']
    va_pct = result['value_area_pct']
    use_letters = result.get('use_letters', False)

    # Group into bins
    bins = defaultdict(list) if use_letters else defaultdict(int)
    for price, data in tpo_dict.items():
        bin_start = int(price // bin_size * bin_size)
        if use_letters:
            bins[bin_start].extend(data)
        else:
            bins[bin_start] += data

    print("\n" + "="*95)
    print(f"{title.center(95)}")
    print(f"Bin size: {bin_size} points | Value Area: {va_pct*100:.0f}% | "
          f"Time bracket: {result.get('period_minutes', 30)} minutes")
    print(f"POC: {poc:.2f}   VAH: {vah:.2f}   VAL: {val:.2f}")
    print("-" * 95)
    print(f"{'Price Bin':<18} {'TPO Count':>10}   {'Visual':<45}  Markers")
    print("-" * 95)

    max_count = max((len(v) if use_letters else v) for v in bins.values()) if bins else 1

    for bin_start in sorted(bins.keys(),reverse=True):
        data = bins[bin_start]
        count = len(data) if use_letters else data

        # Visual part
        if use_letters:
            letters = sorted(set(data))  # unique letters in this bin
            visual = ''.join(letters)[:45]  # limit length
        else:
            bar_length = min(int(count / max_count * 45), 45)
            visual = "#" * bar_length

        bin_end = bin_start + bin_size
        label = f"{bin_start:,.2f} - {bin_end:,.2f}"

        markers = []
        if val <= bin_end and vah >= bin_start:
            markers.append("VA")
        if abs(poc - (bin_start + bin_size/2)) <= bin_size/2:
            markers.append("POC★")

        print(f"{label:<18} {count:10}   {visual:<45}  {' '.join(markers)}")

    print("="*95)
    print(f"Total TPOs: {result['total_tpo']:,} | Profile from {len(tpo_dict):,} price levels")
    print("="*95)


def plot_profile_gui(result: dict, bin_size: int = 10, title: str = "Market Profile"):
    """Nice GUI chart using matplotlib"""
    if not HAS_MATPLOTLIB:
        print("❌ Matplotlib not installed. Run: pip install matplotlib")
        return

    tpo_dict = result['tpo_dict']
    use_letters = result.get('use_letters', False)

    # Always use counts for plotting
    count_dict = {p: len(lst) if use_letters else lst for p, lst in tpo_dict.items()}

    # Bin for plot
    bins = defaultdict(int)
    for price, cnt in count_dict.items():
        bin_start = int(price // bin_size * bin_size)
        bins[bin_start] += cnt

    bin_starts = sorted(bins.keys())
    bin_centers = [b + bin_size/2 for b in bin_starts]
    counts = [bins[b] for b in bin_starts]

    fig, ax = plt.subplots(figsize=(10, 12))
    ax.barh(bin_centers, counts, height=bin_size * 0.8, color='skyblue', edgecolor='black')

    # Value Area shading
    ax.axhspan(result['val'], result['vah'], alpha=0.25, color='yellow', label='Value Area (68%)')
    
    # POC line
    ax.axhline(result['poc'], color='red', linewidth=2.5, linestyle='--', label=f'POC = {result["poc"]:.2f}')
    
    # VAH / VAL lines
    ax.axhline(result['vah'], color='green', linewidth=1.5, linestyle='-', label=f'VAH = {result["vah"]:.2f}')
    ax.axhline(result['val'], color='green', linewidth=1.5, linestyle='-', label=f'VAL = {result["val"]:.2f}')

    ax.set_xlabel('TPO Count')
    ax.set_ylabel('Price')
    ax.set_title(title + f"\nTime bracket: {result.get('period_minutes', 30)} min | Bin: {bin_size} points")
    ax.grid(True, axis='x', alpha=0.3)
    ax.legend(loc='upper right')

    plt.tight_layout()
    plt.show()


# ====================== MAIN ======================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Market Profile (TPO) Calculator - Letters + GUI + Configurable Time Bracket")
    parser.add_argument("csv_files", nargs='+', help="One or more 1-min CSV files")
    parser.add_argument("--tick", type=float, default=0.5, help="Price tick size (default: 0.5)")
    parser.add_argument("--period", type=int, default=30, help="Time bracket in minutes (default: 30)")
    parser.add_argument("--va", type=float, default=0.68, help="Value Area percentage (default: 0.68)")
    parser.add_argument("--bin", type=int, default=10, help="Display bin size in points (default: 10)")
    parser.add_argument("--composite", action="store_true", help="Combine ALL days into ONE composite profile")
    parser.add_argument("--letters", action="store_true", help="Use classic TPO letters (A, B, C...) instead of # bars")
    parser.add_argument("--plot", action="store_true", help="Show beautiful GUI chart (requires matplotlib)")
    args = parser.parse_args()

    if len(args.csv_files) == 0:
        print("❌ Please provide at least one CSV file")
        sys.exit(1)

    # Letters only make sense for single-day (different days have different letters)
    if args.composite and args.letters:
        print("⚠️  Letters mode is disabled in composite mode (different days). Using # bars.")
        args.letters = False

    daily_results = []
    for file in args.csv_files:
        try:
            df = pd.read_csv(file)
            print(f"✅ Loaded {len(df):,} rows from {file}")
            result = compute_market_profile(
                df,
                tick_size=args.tick,
                period_minutes=args.period,
                value_area_pct=args.va,
                use_letters=args.letters
            )
            daily_results.append((file, result))
        except Exception as e:
            print(f"❌ Error reading {file}: {e}")
            sys.exit(1)

    # Composite mode
    if args.composite and len(daily_results) > 1:
        print(f"\n🔄 Building COMPOSITE profile from {len(daily_results)} days...")
        composite_tpo = defaultdict(int)
        for _, res in daily_results:
            count_dict = {p: len(lst) if res['use_letters'] else lst for p, lst in res['tpo_dict'].items()}
            for p, cnt in count_dict.items():
                composite_tpo[p] += cnt

        composite_result = get_key_levels(composite_tpo, args.va)
        composite_result['tpo_dict'] = composite_tpo
        composite_result['use_letters'] = False
        composite_result['period_minutes'] = args.period
        display_profile(composite_result, bin_size=args.bin, title=f"COMPOSITE Market Profile ({len(daily_results)} Days)")

        if args.plot:
            plot_profile_gui(composite_result, bin_size=args.bin, title=f"COMPOSITE Market Profile ({len(daily_results)} Days)")

    # Normal mode (single day or multiple separate profiles)
    else:
        for file, result in daily_results:
            display_profile(result, bin_size=args.bin, title=f"Market Profile - {file}")
            if args.plot:
                plot_profile_gui(result, bin_size=args.bin, title=f"Market Profile - {file}")

    print("\n✅ Done!")
    if args.plot and not HAS_MATPLOTLIB:
        print("   Tip: pip install matplotlib  →  for beautiful GUI charts")