import json
import numpy as np
import matplotlib.pyplot as plt

def simulate_hedging():
    # Load historical nim-large logs
    requests = []
    with open('historical_nim_large_logs.jsonl', 'r') as f:
        for line in f:
            try:
                requests.append(json.loads(line))
            except:
                continue

    # Extract latency pairs (L0, L1)
    data = []
    all_l1 = []
    for r in requests:
        if not r.get('success'): continue
        cands = r.get('candidates_tried', [])
        l0 = cands[0]['latency']
        l1 = None
        if len(cands) >= 2:
            if r['winner'] == cands[1]['name']:
                l1 = r['latency_ms']
            elif cands[1]['ok']:
                l1 = cands[1]['latency']
        
        if l1:
            all_l1.append(l1)
        data.append((l0, l1))

    # Average L1 to use as placeholder for simulations
    avg_l1 = np.median(all_l1) if all_l1 else 30000 

    # Higher resolution: 0.5s steps from 20 to 70
    delays = np.arange(20, 70.5, 0.5)
    no_hedge_rates = []
    waste_rates = []
    benefit_rates = []
    avg_perceived_latencies = []

    total_n = len(data)
    for d_sec in delays:
        d = d_sec * 1000
        f, w, b = 0, 0, 0
        total_latency = 0
        
        for l0, l1_actual in data:
            l1_to_use = l1_actual if l1_actual is not None else avg_l1
            
            if l0 <= d:
                # No hedge triggered
                f += 1
                total_latency += l0
            else:
                # Hedge triggered
                perceived = min(l0, d + l1_to_use)
                total_latency += perceived
                
                if l1_to_use < (l0 - d):
                    b += 1
                else:
                    w += 1
        
        no_hedge_rates.append(f / total_n * 100)
        waste_rates.append(w / total_n * 100)
        benefit_rates.append(b / total_n * 100)
        avg_perceived_latencies.append(total_latency / total_n / 1000) # Convert to seconds

    # Plotting
    fig, ax1 = plt.subplots(figsize=(12, 7))

    # Primary Axis: Rates
    ax1.plot(delays, no_hedge_rates, label='No-Hedge Rate (1st wins < Delay)', color='#2ca02c', linewidth=2, alpha=0.8)
    ax1.plot(delays, waste_rates, label='Waste Rate (1st wins after Delay)', color='#ff7f0e', linewidth=2, alpha=0.8)
    ax1.plot(delays, benefit_rates, label='Benefit Rate (2nd wins faster)', color='#1f77b4', linewidth=2, alpha=0.8)
    ax1.set_xlabel('Hedge Delay (seconds)', fontsize=12)
    ax1.set_ylabel('Percentage of Requests (%)', fontsize=12)
    ax1.set_ylim(0, 100)
    ax1.grid(True, which='both', linestyle='--', alpha=0.3)

    # Secondary Axis: Average Latency
    ax2 = ax1.twinx()
    ax2.plot(delays, avg_perceived_latencies, label='Avg Perceived Latency (s)', color='red', linewidth=3, linestyle='-')
    ax2.set_ylabel('Avg Perceived Latency (seconds)', color='red', fontsize=12)
    ax2.tick_params(axis='y', labelcolor='red')
    # Focus on the relevant latency range
    lat_min, lat_max = min(avg_perceived_latencies), max(avg_perceived_latencies)
    ax2.set_ylim(lat_min - 2, lat_max + 2)

    # Current point marker
    ax1.axvline(x=40, color='gray', linestyle='--', alpha=0.6)
    curr_lat = avg_perceived_latencies[list(delays).index(40.0)]
    ax2.plot(40, curr_lat, 'ro')
    ax2.annotate(f'Current Avg: {curr_lat:.1f}s', xy=(40, curr_lat), xytext=(42, curr_lat + 1),
                 color='red', fontweight='bold', arrowprops=dict(arrowstyle="->", color='red'))

    plt.title('Hedge Delay Impact: Rates vs. User Latency (nim-large, N=6876)', fontsize=14)
    
    # Combined legend
    lines, labels = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines + lines2, labels + labels2, loc='upper left', fontsize=10)
    
    plt.tight_layout()
    plt.savefig('hedging_tradeoff.png', dpi=150)
    print("Updated plot with average latency saved to hedging_tradeoff.png")

if __name__ == "__main__":
    simulate_hedging()
