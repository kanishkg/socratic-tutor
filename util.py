# Utilities.

import random
import datetime
import re

signs = ['+', '-']

def format_eta(elapsed_time, elapsed_steps, total_steps):
    remaining_time = elapsed_time / elapsed_steps * (total_steps - elapsed_steps)
    return str(remaining_time)


def random_id(length=5):
    return "".join(random.choice("0123456789abcdef") for _ in range(length))


# Helper decorator that registers the baseclass under the 'subtypes' attribute
# of the given class.
def register(superclass):
    def decorator(subclass):
        superclass.subtypes[subclass.__name__] = subclass
        return subclass
    return decorator


def now():
    'The current time as string, to be printed in log messages.'
    return datetime.datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")


def filter_problem(problem):
    # remove parsing error of x in denominator
    xids = [i for i, c in enumerate(problem) if c == 'x']
    for _ in range(len(xids)):
        nums = re.findall('[0-9]+', problem)
        ids = [m.start(0) for m in re.finditer('[0-9]+', problem)]
        # ids = [problem.index(n) for n in nums]
        for j, (idx, num) in enumerate(zip(ids, nums)): 
            if idx+len(num) >= len(problem):
                continue
            if problem[idx+len(num)] == 'x':
                if problem[idx-2] == '/':
                    prev_num = [ids[j-1], nums[j-1]]
                    if problem[prev_num[0]-1] != '-':
                        problem = problem[:prev_num[0]]+'('+problem[prev_num[0]:idx+len(num)] + ') * '  + problem[idx+len(num):]
                    else:
                        problem = problem[:prev_num[0]-1]+'('+problem[prev_num[0]-1:idx+len(num)] + ') * '  + problem[idx+len(num):]
                    break
    return problem    

def filter_state(state):
    fact = state.facts[-1]
    if  '+ (+' in fact:
        fact = fact.replace('+ (+', '+ (')
    if '- +' in fact:
        fact = fact = fact.replace('- +', '- ')
    if '(+' in fact:
        fact = fact.replace('(+','(')
    if '+ +' in fact:
        fact = fact.replace('+ +', '+ ')
    if '[+' in fact:
        fact = fact.replace('[+', '[')
    if '- +' in fact:
        fact = fact.replace('- +', '- ')
    if fact[0] == '+':
        fact = fact[1:]
    if '* +' in fact:
        fact = fact.replace('* +', '* ')
    if '= +' in fact:
        fact = fact.replace('= +', '= ')
    if '/ +' in fact:
        fact = fact.replace('/ +', '/ ')
    facts = list(state.facts)
    facts[-1] = fact
    state.facts = tuple(facts)
    # if fact!=init_fact:
    #     print(f'{init_fact} -> {fact}')
    return state

def corrupt_vars(fact):
    # choose to add or delete variables
    prob = random.uniform(0, 1)
    # randomly delete a var from the equation
    nums = re.findall('[0-9]+', fact)
    num_x = fact.count('x')
    if num_x == 1:
        prob = 1.
    if len(nums)-1 == num_x:
        prob = 0.
    if prob < 0.5:
        # get ids of characters in fact
        ids = [i for i, c in enumerate(fact) if c == 'x']
        # randomly choose an id and delete variables
        random.shuffle(ids)
        for idx in ids:
            if idx == 0:
                # fact = '0' + fact[1:]
                continue
            if fact[idx-2] == '*':
                fact = fact[:idx-3] + fact[idx+1:]
            elif fact[idx-1] == '-':
                continue
            elif fact[idx-1] == ' ' or fact[idx-1] == '(':
                # fact = fact[:idx] + '0' + fact[idx+1:]
                continue
            else:
                fact = fact[:idx]+fact[idx+1:]
            break
    else:
        # randomly add a variable to the equation
        nums = re.findall('[0-9]+', fact)
        # get start and end positions of numbers
        ids = [(m.start(0), m.end(0)) for m in re.finditer('[0-9]+', fact)]

        ids_nums = list(zip(ids, nums))
        random.shuffle(ids_nums)
        for i, n in ids_nums:
            if i[1] < len(fact):
                if fact[i[1]] == 'x':
                    continue
                if fact[i[1]] == ']':
                    continue
            if fact[i[0]-1] == '[' or fact[i[0]-2:i[0]] == '[-':
                continue
            if fact[i[0]-1] == '/':
                continue
            fact = fact[:i[1]]+'x'+fact[i[1]:]
            break
    return fact

def corrupt_sigs(fact):
    sigs = [(i, s) for i, s in enumerate(fact) if s in signs]
    random.shuffle(sigs)
    idx, s = sigs[0]
    if s == '-':
        ns = '+'
    elif s == '+':
        ns = '-'
    fact = list(fact)
    fact[idx] = ns
    fact = "".join(fact)
    return fact


def parse_parentheses(fact):
    open_close = {}
    par_stack = []
    for i, c in enumerate(fact):
        if c == '(':
            par_stack.append(i)
        elif c == ')':
            if len(par_stack) == 0:
                raise IndexError("No matching closing parens at: " + str(i))
            open_close[par_stack.pop()] = i
    if len(par_stack) > 0:
        raise IndexError("No matching opening parens at: " + str(par_stack.pop()))
    return open_close



def corrupt_parantheses(fact):
    init_fact = fact
    if '- -' in fact:
        fact = fact = fact.replace('- -', '+ ')
    if '+ -' in fact:
        fact = fact = fact.replace('+ -', '- ')


    init_sides = init_fact.split('=')
    # parse parentheses
    open_close_dict = parse_parentheses(fact)
    
    # remove parentheses from the equation
    topop = []
    for k, v in open_close_dict.items():
        if v-k<=8:
            if re.search('^\(-[0-9]+x\)$', fact[k:v+1]) or re.search('^\(-[0-9]+\)$', fact[k:v+1]):
                topop.append(k)
    for k in topop:
        open_close_dict.pop(k)
    pars = [k for k, v in open_close_dict.items()] + [v for k, v in open_close_dict.items()] 
    fact = [c for i, c in enumerate(fact) if i not in pars]
    fact = ''.join(fact)
    blank_fact = fact
    # process lhs and rhs separately; maybe just corrupt one side?
    lhs, rhs = fact.split('=')
    lhs = lhs.strip()
    rhs = rhs.strip()
    
    # parse signs
    sides = []
    for sid, hs in enumerate([lhs, rhs]):
        divids = [i for i, c in enumerate(hs) if c == '/' and '[' not in hs[max(0,i-6):i]]
        mulids = [i for i, c in enumerate(hs) if c == '*']
        sigids = [i for i, c in enumerate(hs) if c in ['-','+'] and hs[i-1:i+1]!='(-' and 'x' not in hs[i:i+5] and '[' not in hs[max(0,i-3):i]]
        
        # skip if there are are either only pos/neg or only mul/div
        if len(sigids) == 0 or len(mulids)+len(divids) == 0:
            sides.append(init_sides[sid])
            continue

        # start adding paranetheses around the signs
        all_sigs = sorted(divids+mulids+sigids)
        num_par = len(all_sigs)
        for i in range(num_par):
            idx = random.choice(all_sigs)
            prev_idx = next_idx = None
            if all_sigs.index(idx) != 0:
                prev_idx = all_sigs[all_sigs.index(idx)-1]
            if all_sigs.index(idx) != len(all_sigs)-1:
                next_idx = all_sigs[all_sigs.index(idx)+1]
            if prev_idx is not None and next_idx is not None:
                hs = hs[:prev_idx+2] + '(' + hs[prev_idx+2:next_idx-1] + ')' + hs[next_idx-1:]
            elif prev_idx is None and next_idx is None:
                hs = '(' + hs + ')'
            elif prev_idx is None:
                hs = '(' + hs[:next_idx-1] + ')' + hs[next_idx-1:]
            elif next_idx is None:
                hs = hs[:prev_idx+2] + '(' + hs[prev_idx+2:] + ')'
            del all_sigs[all_sigs.index(idx)]
            new_sigs = []
            # update the indices after adding parantheses
            for s in all_sigs:
                if s > idx:
                    new_sigs.append(s+2)
                else:
                    new_sigs.append(s)
            all_sigs = new_sigs
        sides.append(hs)
    fact = sides[0]+' = ' +sides[1]
    return fact
    
def corrupt_state(state):
    final_fact = state.facts[-1]
    success = True
    # choose how to corrupt the equation
    # TODO: add more options like calc corruption, bedmas corruption, etc.
    p = random.uniform(0, 1)
    sigs = [(i, s) for i, s in enumerate(final_fact) if s in signs]
    nums = re.findall('[0-9]+', final_fact)
    num_x = final_fact.count('x')
    if num_x == 1 and len(nums)-1 == num_x:
        p = random.uniform(0, 0.66)
    if len(sigs) == 0:
        p = random.uniform(0.33, 1.)
    if p < 0.33:
        final_fact = corrupt_sigs(final_fact)
    elif p<0.66:
        final_fact = corrupt_parantheses(final_fact)
    else:
        final_fact = corrupt_vars(final_fact)
    if final_fact == state.facts[-1]:
        success = False
    else:
        state.corrupt = True 
    facts = list(state.facts)
    facts[-1] = final_fact
    state.facts = tuple(facts)
    return state, success
