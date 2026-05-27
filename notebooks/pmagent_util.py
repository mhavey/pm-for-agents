import pm4py
import json 
from itertools import groupby
import collections

def get_num_cases(df):
    num_cases = df["case:concept:name"].nunique()
    return num_cases

def dump_one_variant(path):
    return "->".join(path)

def dump_variants(df, show_all=True):
    num_cases = get_num_cases(df)
    variant_occur_cutoff = int(num_cases/10)
    print(f"Number of cases {num_cases}")

    variants=pm4py.get_variants(df, activity_key="concept:name", case_id_key="case:concept:name")
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
        
def deduplicate_agent_stutter(df, case_col='case:concept:name', activity_col='concept:name',
                              time_col='time:timestamp'):
    """
    Removes back-to-back duplicate activities within the same case container.
    Preserves the very first occurrence of the activity.
    """
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

def dump_conformance_diag_token_based_replay(df, net, im, fm):
    replay_results = pm4py.conformance_diagnostics_token_based_replay(df, net, im, fm)
    print("=== NON-COMPLIANT AGENT TRACE AUDIT ===\n")
    
    # Get the order of unique Case IDs as processed by pm4py
    unique_cases = df['case:concept:name'].unique()
    
    for i, result in enumerate(replay_results):
        if not result['trace_is_fit']:
            case_id = unique_cases[i]
            
            # 1. Fix: Read the .label attribute directly from the Transition object
            raw_activities = [
                t.label for t in result['activated_transitions'] 
                if t.label is not None
            ]
            trace_tuple_str = f"({', '.join(raw_activities)})"
            
            # 2. Fix: Apply the same .label reading for the problem transitions
            problem_activities = list(set([
                t.label for t in result['transitions_with_problems'] 
                if t.label is not None
            ]))
            
            # 3. Print the formatted diagnostics
            print(f"❌ Case ID: {case_id} (Index: {i})")
            print(f"   Execution Trace: {trace_tuple_str}")
            
            # Formulate the English explanation of the failure narrative
            if problem_activities:
                flagged_tasks = ", ".join([f"'{task}'" for task in problem_activities])
                
                print(f"   Compliance Issue: The agent broke sequence rules. It attempted to execute ")
                print(f"                     {flagged_tasks} before the prerequisite phase gates ")
                print(f"                     had formally opened or closed.")
            else:
                print(f"   Compliance Issue: Process short-circuited. The agent exited or terminated ")
                print(f"                     the workflow early, leaving active mandatory tasks unexecuted.")
                
            print(f"   Replay Metrics:   Missing Tokens: {result['missing_tokens']} | Stranded Tokens: {result['remaining_tokens']}\n")

import pm4py

def dump_conformance_diagnostics(df, net, im, fm, method='token'):
    """
    Unified conformance diagnostic auditor. 
    Constructs a visual alignment ladder for the 'alignment' method.
    """
    unique_cases = df['case:concept:name'].unique()
    
    if method == 'token':
        results = pm4py.conformance_diagnostics_token_based_replay(df, net, im, fm)
        print("=== NON-COMPLIANT AGENT TRACE AUDIT (TOKEN METHOD) ===\n")
    elif method == 'alignment':
        results = pm4py.conformance_diagnostics_alignments(df, net, im, fm)
        print("=== NON-COMPLIANT AGENT TRACE AUDIT (ALIGNMENT METHOD) ===\n")
    else:
        raise ValueError("Method must be either 'token' or 'alignment'")

    for i, result in enumerate(results):
        is_non_compliant = (method == 'token' and not result['trace_is_fit']) or \
                             (method == 'alignment' and result['fitness'] < 1.0)
        
        if is_non_compliant:
            case_id = unique_cases[i]
            print(f"❌ Case ID: {case_id} (Index: {i})")
            
            # --- METHOD 1: TOKEN-BASED REPLAY ---
            if method == 'token':
                raw_activities = [t.label for t in result['activated_transitions'] if t.label is not None]
                print(f"   Execution Trace: ({', '.join(raw_activities)})")
                
                problem_activities = list(set([t.label for t in result['transitions_with_problems'] if t.label is not None]))
                if problem_activities:
                    flagged_tasks = ", ".join([f"'{task}'" for task in problem_activities])
                    print(f"   Compliance Issue: The agent broke sequence rules. It attempted to execute ")
                    print(f"                     {flagged_tasks} before prerequisite phase gates opened/closed.")
                else:
                    print(f"   Compliance Issue: Process short-circuited. The agent terminated early, ")
                    print(f"                     leaving active mandatory tasks unexecuted.")
                
                print(f"   Replay Metrics:     Missing Tokens: {result['missing_tokens']} | Stranded Tokens: {result['remaining_tokens']}\n")
            
            # --- METHOD 2: ALIGNMENTS (WITH VISUAL LADDER) ---
            elif method == 'alignment':
                alignment = result['alignment']
                
                # Build the row strings for the visual alignment chart
                log_row = []
                model_row = []
                status_row = []
                
                for move in alignment:
                    log_act = move[0] if move[0] != '>>' else '>>'
                    model_act = move[1] if move[1] != '>>' else '>>'
                    
                    # Calculate padding based on the longest string length to keep columns perfectly straight
                    col_width = max(len(str(log_act)), len(str(model_act)), 4)
                    
                    log_row.append(f"{str(log_act):<{col_width}}")
                    model_row.append(f"{str(model_act):<{col_width}}")
                    
                    # Add a visual indicator status between rows
                    if move[0] == '>>':
                        status_row.append(f"{'▼ SKIP':<{col_width}}") # Move on Model
                    elif move[1] == '>>':
                        status_row.append(f"{'▲ DRIFT':<{col_width}}") # Move on Log
                    else:
                        status_row.append(f"{'  |':<{col_width}}") # Synchronous Move

                # Print the beautiful text-based alignment visualization
                print("   [ VISUAL ALIGNMENT LADDER ]")
                print(f"       Log (Reality):   [ {' | '.join(log_row)} ]")
                print(f"                        {f'   {"   ".join(status_row)}'}")
                print(f"       Model (Intent):  [ {' | '.join(model_row)} ]\n")
                
                # Summarize the metrics
                print(f"   Alignment Metrics:  Fitness Score: {result['fitness']:.2f} | Total Alignment Cost: {result['cost']}\n")
                print("-" * 80 + "\n")