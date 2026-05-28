import pm4py
import json 
from itertools import groupby
from collections import defaultdict
import collections

def get_num_cases(df):
    num_cases = df["case:concept:name"].nunique()
    return num_cases

def dump_one_variant(path):
    return "->".join(path)

def dump_variants(df, variants, show_all=True):
    num_cases = get_num_cases(df)
    variant_occur_cutoff = int(num_cases/10)
    print(f"Number of cases {num_cases}")

    sorted_desc = sorted([(count, path) for path, count in variants.items()], key=lambda x: x[0], reverse=True)
    
    print("\nFrequent variants")
    for count, path in sorted_desc:
        if count >= variant_occur_cutoff:
            print(f"{count} {dump_one_variant(path)}")

    if show_all:
        print("\nAll variants")
        for count, path in sorted_desc:
            print(f"{count} {dump_one_variant(path)}")

    print("\nAll variants, stutter removed")
    collapsed_variants = collections.defaultdict(int)
    for count, path in sorted_desc:
        shortened_path = tuple(key for key, _group in groupby(path))
        collapsed_variants[shortened_path] += count
    
    final_distribution = dict(sorted(collapsed_variants.items(), key=lambda x: x[1], reverse=True))
    for path, count in final_distribution.items():
        print(f"{count} {dump_one_variant(path)}")
        
def deduplicate_agent_stutter(df):
    """
    Removes back-to-back duplicate activities within the same case container.
    Preserves the very first occurrence of the activity.
    """
    case_col='case:concept:name'
    activity_col='concept:name'
    time_col='time:timestamp'
    
    # 1. Ensure the log is strictly sorted chronologically
    df_sorted = df.sort_values(by=[case_col, time_col]).copy()
    
    # 2. Look one row backward WITHIN the same case boundary
    previous_activity = df_sorted.groupby(case_col)[activity_col].shift(1)
    
    # 3. Create a boolean mask where the current activity is NOT equal to the previous one
    # (The .isna() ensures we keep the very first event of the case)
    clean_mask = (df_sorted[activity_col] != previous_activity) | (previous_activity.isna())
    
    # 4. Filter the dataframe
    df_clean = df_sorted[clean_mask].reset_index(drop=True)
    
    # Print operational metrics to stdout
    dropped_rows = len(df) - len(df_clean)
    print(f"--- Data Cleaning Metrics ---")
    print(f"Original Event Count: {len(df)}")
    print(f"Cleaned Event Count:  {len(df_clean)}")
    print(f"Dropped Stutter Rows: {dropped_rows} ({ (dropped_rows/len(df))*100:.2f}%)")
    
    return df_clean

def dump_conformance_diagnostics(df, results, method='token'):
    """
    Unified conformance diagnostic auditor with error consolidation.
    Aggregates identical failure signatures to prevent duplicate reporting.
    """
    unique_cases = df['case:concept:name'].unique()
    
    # Dictionary to aggregate identical errors
    # Key: A structural representation of the error, Value: List of (index, case_id, result_dict)
    error_registry = defaultdict(list)

    for i, result in enumerate(results):
        is_non_compliant = (method == 'token' and not result['trace_is_fit']) or \
                             (method == 'alignment' and result['fitness'] < 1.0)
        
        if is_non_compliant:
            case_id = unique_cases[i]
            
            if method == 'token':
                # Extract the ordered activity labels to construct the execution path
                variant_path = [t.label for t in result['activated_transitions'] if t.label is not None]
                
                # Create a unique key based on problem activities, token metrics, and the variant path
                problem_activities = tuple(sorted(list(set([t.label for t in result['transitions_with_problems'] if t.label is not None]))))
                error_key = (problem_activities, result['missing_tokens'], result['remaining_tokens'], tuple(variant_path))
                
            elif method == 'alignment':
                # Create a unique key based on the exact sequence of tuples in the alignment
                error_key = tuple(result['alignment'])
                
            error_registry[error_key].append((i, case_id, result))

    # --- REPORTING CONSOLIDATED RESULTS ---
    total_distinct_errors = len(error_registry)
    print(f"=== NON-COMPLIANT AGENT TRACE AUDIT ({method.upper()} METHOD) ===")
    print(f"Found {total_distinct_errors} distinct non-compliance patterns:\n")

    for error_key, occurrences in error_registry.items():
        count = len(occurrences)
        # Grab the first occurrence as a representative example for visualization
        first_idx, first_case_id, representative_result = occurrences[0]
        
        # Collect all case IDs sharing this exact issue
        all_case_ids = [occ[1] for occ in occurrences]
        case_list_str = ", ".join(all_case_ids[:3]) + (f" ... (+{count-3} more)" if count > 3 else "")
        
        print(f"⚠️  Pattern Detected: {count} occurrences")
        print(f"   Impacted Case IDs: [{case_list_str}]")
        
        # --- METHOD 1: CONSOLIDATED TOKEN REPLAY ---
        if method == 'token':
            # Extract variant path from our composite error key and print using custom dumper
            variant_tuple = error_key[3]
            formatted_variant = dump_one_variant(variant_tuple) if variant_tuple else "(Empty Trace)"
            print(f"   Discovered Variant: {formatted_variant}")
            
            problem_activities = error_key[0]
            if problem_activities:
                flagged_tasks = ", ".join([f"'{task}'" for task in problem_activities])
                print(f"   Compliance Issue: The agent broke sequence rules. It attempted to execute ")
                print(f"                     {flagged_tasks} before prerequisite phase gates opened/closed.")
            else:
                print(f"   Compliance Issue: Process short-circuited. The agent terminated early, ")
                print(f"                     leaving active mandatory tasks unexecuted.")
            
            print(f"   Replay Metrics:     Missing Tokens: {representative_result['missing_tokens']} | Stranded Tokens: {representative_result['remaining_tokens']}\n")
        
        # --- METHOD 2: CONSOLIDATED ALIGNMENTS ---
        elif method == 'alignment':
            alignment = representative_result['alignment']
            
            log_row, model_row, status_row = [], [], []
            for move in alignment:
                log_act = move[0] if move[0] != '>>' else '>>'
                model_act = move[1] if move[1] != '>>' else '>>'
                
                col_width = max(len(str(log_act)), len(str(model_act)), 4)
                log_row.append(f"{str(log_act):<{col_width}}")
                model_row.append(f"{str(model_act):<{col_width}}")
                
                if move[0] == '>>':
                    status_row.append(f"{'▼ SKIP':<{col_width}}")
                elif move[1] == '>>':
                    status_row.append(f"{'▲ DRIFT':<{col_width}}")
                else:
                    status_row.append(f" {'|':<{col_width-1}}")

            print("   [ VISUAL ALIGNMENT LADDER ]")
            print(f"       Log (Reality):   [ {' | '.join(log_row)} ]")
            print(f"                        {f'   {"   ".join(status_row)}'}")
            print(f"       Model (Intent):  [ {' | '.join(model_row)} ]\n")
            print(f"   Alignment Metrics:  Fitness Score: {representative_result['fitness']:.2f} | Total Alignment Cost: {representative_result['cost']}\n")
            
        print("-" * 80 + "\n")