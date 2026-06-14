import json
import statistics
import sys

def analyze_logs(file_path):
    requests = []
    try:
        with open(file_path, 'r') as f:
            for line in f:
                try:
                    requests.append(json.loads(line))
                except:
                    continue
    except FileNotFoundError:
        print(f"File {file_path} not found.")
        return

    total = len(requests)
    if total == 0:
        return

    # User perceived latencies
    total_latencies = []
    first_attempt_latencies = []
    
    waste_count = 0
    benefit_count = 0
    
    for r in requests:
        if not r.get('success'):
            continue
        
        candidates = r.get('candidates_tried', [])
        winner_name = r.get('winner')
        
        # Calculate winner index
        winner_idx = -1
        for i, c in enumerate(candidates):
            if c.get('ok') and c.get('name') == winner_name and c.get('latency') == r.get('latency_ms'):
                winner_idx = i
                break
        
        if winner_idx == -1:
            # Fallback to just name match if latency mismatch (shouldn't happen)
            for i, c in enumerate(candidates):
                if c.get('ok') and c.get('name') == winner_name:
                    winner_idx = i
                    break
        
        # Assume interval is 40 for nim-large
        interval = 40000
        user_latency = r['latency_ms'] + (winner_idx * interval)
        total_latencies.append(user_latency)
        
        if len(candidates) == 1:
            first_attempt_latencies.append(r['latency_ms'])
        else:
            if winner_idx == 0:
                waste_count += 1
            else:
                benefit_count += 1

    print(f"File: {file_path}")
    print(f"Total nim-large requests: {total}")
    print(f"User Perceived Latency P50: {statistics.median(total_latencies):.0f}ms")
    print(f"User Perceived Latency P90: {statistics.quantiles(total_latencies, n=10)[8]:.0f}ms")
    print(f"User Perceived Latency P95: {statistics.quantiles(total_latencies, n=20)[18]:.0f}ms")
    
    if first_attempt_latencies:
        print(f"First-attempt-only Latency (no hedging triggered):")
        print(f"  - Count: {len(first_attempt_latencies)} ({len(first_attempt_latencies)/total*100:.1f}%)")
        print(f"  - P50: {statistics.median(first_attempt_latencies):.0f}ms")
        print(f"  - P90: {statistics.quantiles(first_attempt_latencies, n=10)[8]:.0f}ms")
        print(f"  - P99: {statistics.quantiles(first_attempt_latencies, n=100)[98]:.0f}ms")

    print(f"Hedging triggered: {waste_count + benefit_count} ({(waste_count + benefit_count)/total*100:.1f}%)")
    print(f"  - Waste (1st eventually won): {waste_count} ({waste_count/total*100:.1f}%)")
    if waste_count > 0:
        waste_latencies = [r['latency_ms'] for r in requests if r.get('success') and len(r.get('candidates_tried', [])) > 1 and r.get('winner') == r.get('candidates_tried')[0]['name']]
        print(f"    - 1st candidate latency P50 in waste: {statistics.median(waste_latencies):.0f}ms")
        print(f"    - 1st candidate latency P90 in waste: {statistics.quantiles(waste_latencies, n=10)[8]:.0f}ms")
    
    print(f"  - Benefit (Later won): {benefit_count} ({benefit_count/total*100:.1f}%)")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        analyze_logs(sys.argv[1])
    else:
        analyze_logs('nim_large_logs.jsonl')
