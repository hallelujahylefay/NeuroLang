import logging
from functools import reduce
from itertools import chain, combinations

import numpy as np

from .. import relational_algebra_provenance as rap
from ..datalog.expression_processing import (
    UnifyVariableEqualities,
    flatten_query,
)
from ..datalog.translate_to_named_ra import TranslateToNamedRA
from ..exceptions import NeuroLangException, NonLiftableException
from ..expression_walker import (
    PatternWalker,
    ReplaceExpressionWalker,
    add_match,
)
from ..expressions import (
    Constant,
    FunctionApplication,
    Symbol,
    TypedSymbolTableMixin,
)
from ..logic import (
    FALSE,
    Conjunction,
    Disjunction,
    ExistentialPredicate,
    Implication,
    NaryLogicOperator,
    Union,
)
from ..logic.expression_processing import (
    extract_logic_atoms,
    extract_logic_free_variables,
)
from ..logic.transformations import (
    CollapseConjunctions,
    CollapseDisjunctions,
    GuaranteeConjunction,
    MakeExistentialsImplicit,
    RemoveTrivialOperations,
)
from ..relational_algebra import (
    BinaryRelationalAlgebraOperation,
    ColumnStr,
    NamedRelationalAlgebraFrozenSet,
    NAryRelationalAlgebraOperation,
    UnaryRelationalAlgebraOperation,
    str2columnstr_constant,
)
from ..relational_algebra_provenance import ProvenanceAlgebraSet
from ..utils import OrderedSet, log_performance
from .containment import is_contained
from .exceptions import NotEasilyShatterableError
from .probabilistic_ra_utils import (
    DeterministicFactSet,
    NonLiftable,
    ProbabilisticChoiceSet,
    ProbabilisticFactSet,
    generate_probabilistic_symbol_table_for_query,
)
from .probabilistic_semiring_solver import ProbSemiringSolver
from .query_resolution import (
    lift_solve_marg_query,
    reintroduce_unified_head_terms,
)
from .shattering import shatter_easy_probfacts
from .small_dichotomy_theorem_based_solver import (
    RAQueryOptimiser,
    lift_optimization_for_choice_predicates,
)
from .transforms import (
    add_existentials_except,
    convert_rule_to_ucq,
    minimize_ucq_in_cnf,
    minimize_ucq_in_dnf,
    unify_existential_variables,
)

LOG = logging.getLogger(__name__)


__all__ = [
    "dalvi_suciu_lift",
    "solve_succ_query",
    "solve_marg_query",
]

RTO = RemoveTrivialOperations()


class LiftedQueryProcessingSolver(
    rap.DisjointProjectMixin,
    rap.WeightedNaturalJoinSolverMixin,
    ProbSemiringSolver,
):
    pass


def solve_succ_query(query, cpl_program):
    """
    Solve a SUCC query on a CP-Logic program.

    Parameters
    ----------
    query : Implication
        SUCC query of the form `ans(x) :- P(x)`.
    cpl_program : CPLogicProgram
        CP-Logic program on which the query should be solved.

    Returns
    -------
    ProvenanceAlgebraSet
        Provenance set labelled with probabilities for each tuple in the result
        set.

    """
    with log_performance(
        LOG,
        "Preparing query %s",
        init_args=(query.consequent.functor.name,),
    ):
        flat_query_body = flatten_query(query.antecedent, cpl_program)

    if flat_query_body == FALSE or (
        isinstance(flat_query_body, Conjunction)
        and any(conjunct == FALSE for conjunct in flat_query_body.formulas)
    ):
        head_var_names = tuple(
            term.name for term in query.consequent.args
            if isinstance(term, Symbol)
        )
        return ProvenanceAlgebraSet(
            NamedRelationalAlgebraFrozenSet(("_p_",) + head_var_names),
            ColumnStr("_p_"),
        )

    with log_performance(LOG, "Translation to extensional plan"):
        flat_query = Implication(query.consequent, flat_query_body)
        flat_query_body = lift_optimization_for_choice_predicates(
            flat_query_body, cpl_program
        )
        flat_query = Implication(query.consequent, flat_query_body)
        symbol_table = generate_probabilistic_symbol_table_for_query(
            cpl_program, flat_query_body
        )
        unified_query = UnifyVariableEqualities().walk(flat_query)
        try:
            shattered_query = symbolic_shattering(unified_query, symbol_table)
        except NotEasilyShatterableError:
            shattered_query = unified_query
        ra_query = dalvi_suciu_lift(shattered_query, symbol_table)
        if not is_pure_lifted_plan(ra_query):
            LOG.info(
                "Query not liftable %s",
                shattered_query
            )
            raise NonLiftableException(
                "Query %s not liftable, algorithm can't be applied",
                query
            )
        ra_query = reintroduce_unified_head_terms(
            ra_query, flat_query, unified_query
        )
        ra_query = RAQueryOptimiser().walk(ra_query)

    with log_performance(LOG, "Run RAP query"):
        solver = LiftedQueryProcessingSolver(symbol_table)
        prob_set_result = solver.walk(ra_query)

    return prob_set_result


def solve_marg_query(rule, cpl):
    return lift_solve_marg_query(rule, cpl, solve_succ_query)


def dalvi_suciu_lift(rule, symbol_table):
    '''
    Translation from a datalog rule which allows disjunctions in the body
    to a safe plan according to [1]_. Non-liftable segments are identified
    by the `NonLiftable` expression.

    [1] Dalvi, N. & Suciu, D. The dichotomy of probabilistic inference
    for unions of conjunctive queries. J. ACM 59, 1–87 (2012).
    '''
    if isinstance(rule, Implication):
        rule = convert_rule_to_ucq(rule)
    rule = RTO.walk(rule)
    if isinstance(rule, FunctionApplication):
        return TranslateToNamedRA().walk(rule)
    elif (
        all(
            is_atom_a_deterministic_relation(atom, symbol_table)
            for atom in extract_logic_atoms(rule)
        )
    ):
        free_vars = extract_logic_free_variables(rule)
        rule = MakeExistentialsImplicit().walk(rule)
        result = TranslateToNamedRA().walk(rule)
        proj_cols = tuple(Constant(ColumnStr(v.name)) for v in free_vars)
        return rap.Projection(result, proj_cols)

    rule_cnf = minimize_ucq_in_cnf(rule)
    connected_components = symbol_connected_components(rule_cnf)
    if len(connected_components) > 1:
        return components_plan(
            connected_components, rap.NaturalJoin, symbol_table
        )
    elif len(rule_cnf.formulas) > 1:
        return inclusion_exclusion_conjunction(rule_cnf, symbol_table)

    rule_dnf = minimize_ucq_in_dnf(rule)
    connected_components = symbol_connected_components(rule_dnf)
    if len(connected_components) > 1:
        return components_plan(connected_components, rap.Union, symbol_table)
    else:
        has_svs, plan = has_separator_variables(rule_dnf, symbol_table)
        if has_svs:
            return plan
        else:
            has_safe_plan, plan = disjoint_project(rule_dnf, symbol_table)
            if has_safe_plan:
                return plan

    return NonLiftable(rule)


def disjoint_project(rule_dnf, symbol_table):
    """
    Rule that extends the lifted query processing algorithm to handle
    Block-Independent Disjoint (BID) tables which encode mutual exclusivity
    assumptions on top of the tuple-independent assumption of probabilistic
    tables.

    Modifications to the lifted query processing algorithm that are necessary
    to extend it to BID tables are detailed in section 4.3.1 of [1]_.

    Two variants of the rule exist: one for conjunctive queries, and one for
    disjunctive queries.

    [1] Suciu, Dan, Dan Olteanu, Christopher Ré, and Christoph Koch, eds.
    Probabilistic Databases. Synthesis Lectures on Data Management 16. San
    Rafael, Calif.: Morgan & Claypool Publ, 2011.

    """
    if len(rule_dnf.formulas) == 1:
        conjunctive_query = rule_dnf.formulas[0]
        return disjoint_project_conjunctive_query(
            conjunctive_query, symbol_table
        )
    return disjoint_project_disjunctive_query(rule_dnf, symbol_table)


def disjoint_project_conjunctive_query(conjunctive_query, symbol_table):
    """
    First variant of the disjoint project on a CQ in CNF.

    A disjoint project operator is applied whenever any of the atoms in the CQ
    has only constants in its key positions and has at least one variable in a
    non-key position.

    Note: as variables are removed through shattering, this only applies to
    all probabilistic choice variables.

    """
    free_variables = extract_logic_free_variables(conjunctive_query)
    query = MakeExistentialsImplicit().walk(conjunctive_query)
    query = CollapseConjunctions().walk(query)
    query = GuaranteeConjunction().walk(query)
    atoms_with_constants_in_all_key_positions = set(
        atom
        for atom in query.formulas
        if is_probabilistic_atom_with_constants_in_all_key_positions(
            atom, symbol_table
        )
    )
    if not atoms_with_constants_in_all_key_positions:
        return False, None
    nonkey_variables = set.union(
        *(
            extract_nonkey_variables(atom, symbol_table)
            for atom in atoms_with_constants_in_all_key_positions
        )
    )
    symbol_table = symbol_table.copy()
    for atom in atoms_with_constants_in_all_key_positions:
        if not isinstance(symbol_table[atom.functor], ProbabilisticChoiceSet):
            raise NeuroLangException(
                "Any atom with constants in all its key positions should be "
                "a probabilistic choice atom"
            )
        symbol_table[atom.functor] = ProbabilisticFactSet(
            symbol_table[atom.functor].relation,
            symbol_table[atom.functor].probability_column,
        )
    conjunctive_query = add_existentials_except(
        query, free_variables | nonkey_variables
    )
    plan = dalvi_suciu_lift(conjunctive_query, symbol_table)
    attributes = tuple(
        str2columnstr_constant(v.name)
        for v in free_variables
    )
    plan = rap.DisjointProjection(plan, attributes)
    return True, plan


def disjoint_project_disjunctive_query(disjunctive_query, symbol_table):
    """
    Second variant of the disjoint project on a UCQ in DNF.

    This rule applies whenever the given query Q can be written as Q = Q1 v Q',
    where the conjunctive query Q1 has an atom where all key attribute are
    constants, and Q' is any other UCQ.

    Then we return P(Q) = P(Q1) + P(Q') - P(Q1 ∧ Q') with subsequent recursive
    calls to the resolution algorithms. Note that a disjoint project should be
    applied during the calculation of P(Q1) and P(Q1 ∧ Q').

    """
    matching_disjuncts = (
        _get_disjuncts_containing_atom_with_all_key_attributes(
            disjunctive_query, symbol_table
        )
    )
    if not matching_disjuncts:
        return False, None
    for disjunct in matching_disjuncts:
        has_safe_plan, plan = _apply_disjoint_project_ucq_rule(
            disjunctive_query, disjunct, symbol_table
        )


def _get_disjuncts_containing_atom_with_all_key_attributes(ucq, symbol_table):
    matching_disjuncts = set()
    for disjunct in ucq.formulas:
        rewritten_ucq = MakeExistentialsImplicit().walk(disjunct)
        rewritten_ucq = CollapseDisjunctions().walk(rewritten_ucq)
        rewritten_ucq = GuaranteeConjunction().walk(rewritten_ucq)
        if any(
            is_probabilistic_atom_with_constants_in_all_key_positions(
                atom, symbol_table
            )
            for atom in rewritten_ucq.formulas
        ):
            matching_disjuncts.add(disjunct)
    return matching_disjuncts


def _apply_disjoint_project_ucq_rule(
    disjunctive_query: Union,
    disjunct: Conjunction,
    symbol_table: TypedSymbolTableMixin,
):
    free_vars = extract_logic_free_variables(disjunctive_query)
    head = add_existentials_except(disjunct, free_vars)
    head_plan = dalvi_suciu_lift(head, symbol_table)
    if isinstance(head_plan, NonLiftable):
        return False, None
    tail = add_existentials_except(
        Conjunction(tuple(disjunctive_query.formulas[:1])),
        free_vars,
    )
    tail_plan = dalvi_suciu_lift(tail, symbol_table)
    if isinstance(tail_plan, NonLiftable):
        return False, None
    head_and_tail = add_existentials_except(
        Conjunction(disjunctive_query.formulas), free_vars
    )
    head_and_tail_plan = dalvi_suciu_lift(head_and_tail, symbol_table)
    if isinstance(head_and_tail_plan, NonLiftable):
        return False, None
    return True, rap.WeightedNaturalJoin(
        (head_plan, tail_plan, head_and_tail_plan),
        (Constant(1), Constant(1), Constant(-1)),
    )


def extract_nonkey_variables(atom, symbol_table):
    """
    Get all variables in the atom that occur on non-key attributes.

    Makes the assumption that the atom is probabilistic.

    As we only support probabilistic choices and not all BID tables, this can
    only be a variable occurring in a probabilistic choice.

    """
    if is_atom_a_probabilistic_choice_relation(atom, symbol_table):
        return {arg for arg in atom.args if isinstance(arg, Symbol)}
    return set()


def has_separator_variables(query, symbol_table):
    """Returns true if `query` has a separator variable and the plan.

    According to Dalvi and Suciu [1]_ if `query` is in DNF,
    a variable z is called a separator variable if Q starts with ∃z,
    that is, Q = ∃z.Q1, for some query expression Q1, and (a) z
    is a root variable (i.e. it appears in every atom),
    (b) for every relation symbol R, there exists an attribute (R, iR)
    such that every atom with symbol R has z in position iR. This is
    equivalent, in datalog syntax, to Q ≡ Q0←∃z.Q1.

    Also, according to Suciu [2]_ the dual is also true,
    if `query` is in CNF i.e. the separation variable z needs to
    be universally quantified, that is Q = ∀x.Q1. But this is not
    implemented.

    [1] Dalvi, N. & Suciu, D. The dichotomy of probabilistic inference
    for unions of conjunctive queries. J. ACM 59, 1–87 (2012).
    [2] Suciu, D. Probabilistic Databases for All. in Proceedings of the
    39th ACM SIGMOD-SIGACT-SIGAI Symposium on Principles of Database Systems
    19–31 (ACM, 2020).

    Parameters
    ----------
    query : LogicExpression
        UCQ to check if it can have a plan based on a separator variable
        strategy.
    symbol_table : dict
        dictionary of symbols and probabilistic/deterministic fact sets.

    Returns
    -------
    boolean, Expression
        Returns true and the plan if the query has separation variables,
        if not, False and None.
    """
    svs, expression = find_separator_variables(query, symbol_table)
    if len(svs) > 0:
        return True, separator_variable_plan(
            expression, svs, symbol_table
        )
    else:
        return False, None


def find_separator_variables(query, symbol_table):
    '''
    According to Dalvi and Suciu [1]_ if `query` is rewritten in prenex
    normal form (PNF) with a DNF matrix, then a variable z is called a
    separator variable if Q starts with ∃z that is, Q = ∃z.Q1, for some
    query expression Q1, and:
      a. z is a root variable (i.e. it appears in every atom); and
      b. for every relation symbol R in Q1, there exists an attribute (R, iR)
      such that every atom with symbol R has z in position iR.

    This algorithm assumes that Q1 can't be splitted into independent
    formulas.

    .. [1] Dalvi, N. & Suciu, D. The dichotomy of probabilistic inference
    for unions of conjunctive queries. J. ACM 59, 1–87 (2012).
    .. [2] Suciu, D. Probabilistic Databases for All. in Proceedings of the
    39th ACM SIGMOD-SIGACT-SIGAI Symposium on Principles of Database Systems
    19–31 (ACM, 2020).
    '''
    exclude_variables = extract_logic_free_variables(query)
    query = unify_existential_variables(query)

    if isinstance(query, NaryLogicOperator):
        formulas = query.formulas
    else:
        formulas = [query]

    candidates = extract_probabilistic_root_variables(formulas, symbol_table)
    all_atoms = extract_logic_atoms(query)

    separator_variables = set()
    for var in candidates:
        atom_positions = {}
        for atom in all_atoms:
            functor = atom.functor
            pos_ = {i for i, v in enumerate(atom.args) if v == var}
            if any(
                pos_.isdisjoint(pos)
                for pos in atom_positions.setdefault(functor, [])
            ):
                break
            atom_positions[functor].append(pos_)
        else:
            separator_variables.add(var)

    return separator_variables - exclude_variables, query


def extract_probabilistic_root_variables(formulas, symbol_table):
    candidates = None
    for formula in formulas:
        probabilistic_atoms = OrderedSet(
            atom
            for atom in extract_logic_atoms(formula)
            if not is_atom_a_deterministic_relation(atom, symbol_table)
        )
        if len(probabilistic_atoms) == 0:
            continue
        root_variables = reduce(
            lambda y, x: set(x.args) & y,
            probabilistic_atoms[1:],
            set(probabilistic_atoms[0].args)
        )
        pchoice_atoms = OrderedSet(
            atom
            for atom in probabilistic_atoms
            if is_atom_a_probabilistic_choice_relation(atom, symbol_table)
        )
        # all variables occurring in probabilistic choices cannot occur in key
        # positions, as probabilistic choice relations have no key attribute
        # (making their respective tuples mutually exclusive)
        nonkey_variables = set().union(*(set(a.args) for a in pchoice_atoms))
        # variables occurring in non-key positions cannot be root variables
        # because root variables must occur in every atom in a key position
        root_variables -= nonkey_variables
        if candidates is None:
            candidates = root_variables
        else:
            candidates &= root_variables
    if candidates is None:
        candidates = set()
    return candidates


class IsPureLiftedPlan(PatternWalker):
    @add_match(NonLiftable)
    def non_liftable(self, expression):
        return False

    @add_match(NAryRelationalAlgebraOperation)
    def nary(self, expression):
        return all(
            self.walk(relation)
            for relation in expression.relations
        )

    @add_match(BinaryRelationalAlgebraOperation)
    def binary(self, expression):
        return (
            self.walk(expression.relation_left) &
            self.walk(expression.relation_right)
        )

    @add_match(UnaryRelationalAlgebraOperation)
    def unary(self, expression):
        return self.walk(expression.relation)

    @add_match(...)
    def other(self, expression):
        return True


def is_pure_lifted_plan(query):
    return IsPureLiftedPlan().walk(query)


def separator_variable_plan(expression, separator_variables, symbol_table):
    """Generate RAP plan for the logic query expression assuming it has
    the given separator varialbes.

    Parameters
    ----------
    expression : LogicExpression
        expression to generate the plan for.
    separator_variables : Set[Symbol]
        separator variables for expression.
    symbol_table : mapping of symbol to probabilistic table.
        mapping used to figure out specific strategies according to
        the probabilistic table type.

    Returns
    -------
    RelationalAlgebraOperation
        plan for the logic expression.
    """
    variables_to_project = extract_logic_free_variables(expression)
    expression = MakeExistentialsImplicit().walk(expression)
    existentials_to_add = (
        extract_logic_free_variables(expression) -
        variables_to_project -
        separator_variables
    )
    for v in existentials_to_add:
        expression = ExistentialPredicate(v, expression)
    return rap.IndependentProjection(
        dalvi_suciu_lift(expression, symbol_table),
        tuple(
            str2columnstr_constant(v.name)
            for v in variables_to_project
        )
    )


def symbol_connected_components(expression):
    if not isinstance(expression, NaryLogicOperator):
        raise ValueError(
            "Connected components can only be computed "
            "for n-ary logic operators."
        )
    c_matrix = symbol_co_occurence_graph(expression)
    components = connected_components(c_matrix)

    operation = type(expression)
    return [
        operation(tuple(expression.formulas[i] for i in component))
        for component in components
    ]


def connected_components(adjacency_matrix):
    """Connected components of an undirected graph.

    Parameters
    ----------
    adjacency_matrix : numpy.ndarray
        squared array representing the adjacency
        matrix of an undirected graph.

    Returns
    -------
    list of integer sets
        connected components of the graph.
    """
    node_idxs = set(range(adjacency_matrix.shape[0]))
    components = []
    while node_idxs:
        idx = node_idxs.pop()
        component = {idx}
        component_follow = [idx]
        while component_follow:
            idx = component_follow.pop()
            idxs = set(adjacency_matrix[idx].nonzero()[0]) - component
            component |= idxs
            component_follow += idxs
        components.append(component)
        node_idxs -= component
    return components


def symbol_co_occurence_graph(expression):
    """Symbol co-ocurrence graph expressed as
    an adjacency matrix.

    Parameters
    ----------
    expression : NAryLogicExpression
        logic expression for which the adjacency matrix is computed.

    Returns
    -------
    numpy.ndarray
        squared binary array where a component is 1 if there is a
        shared predicate symbol between two subformulas of the
        logic expression.
    """
    c_matrix = np.zeros((len(expression.formulas),) * 2)
    for i, formula in enumerate(expression.formulas):
        atom_symbols = set(a.functor for a in extract_logic_atoms(formula))
        for j, formula_ in enumerate(expression.formulas[i + 1:]):
            atom_symbols_ = set(
                a.functor for a in extract_logic_atoms(formula_)
            )
            if not atom_symbols.isdisjoint(atom_symbols_):
                c_matrix[i, i + 1 + j] = 1
                c_matrix[i + 1 + j, i] = 1
    return c_matrix


def variable_co_occurrence_graph(expression):
    """Free variable co-ocurrence graph expressed as
    an adjacency matrix.

    Parameters
    ----------
    expression : NAryLogicExpression
        logic expression for which the adjacency matrixis computed.

    Returns
    -------
    numpy.ndarray
        squared binary array where a component is 1 if there is a
        shared free variable between two subformulas of the logic expression.
    """
    c_matrix = np.zeros((len(expression.formulas),) * 2)
    for i, formula in enumerate(expression.formulas):
        free_variables = extract_logic_free_variables(formula)
        for j, formula_ in enumerate(expression.formulas[i + 1:]):
            free_variables_ = extract_logic_free_variables(formula_)
            if not free_variables.isdisjoint(free_variables_):
                c_matrix[i, i + 1 + j] = 1
                c_matrix[i + 1 + j, i] = 1
    return c_matrix


def components_plan(components, operation, symbol_table):
    formulas = []
    for component in components:
        formulas.append(dalvi_suciu_lift(component, symbol_table))
    return reduce(operation, formulas[1:], formulas[0])


def inclusion_exclusion_conjunction(expression, symbol_table):
    """Produce a RAP query plan for the conjunction logic query
    based on the inclusion exclusion formula.

    Parameters
    ----------
    expression : Conjunction
        logic expression to produce a plan for.
    symbol_table : mapping of symbol to probabilistic table.
        mapping used to figure out specific strategies according to
        the probabilistic table type.

    Returns
    -------
    RelationalAlgebraOperation
        plan for the logic expression.
    """
    formula_powerset = []
    for formula in powerset(expression.formulas):
        if len(formula) == 0:
            continue
        elif len(formula) == 1:
            formula_powerset.append(formula[0])
        else:
            formula_powerset.append(Disjunction(tuple(formula)))
    formulas_weights = _formulas_weights(formula_powerset)
    new_formulas, weights = zip(*(
        (dalvi_suciu_lift(formula, symbol_table), weight)
        for formula, weight in formulas_weights.items()
        if weight != 0
    ))

    return rap.WeightedNaturalJoin(
        tuple(new_formulas),
        tuple(Constant[int](w) for w in weights)
    )


def _formulas_weights(formula_powerset):
    # Using list set instead of a dictionary
    # due to a strange bug in dictionary lookups with Expressions
    # as keys
    formula_containments = [
        set()
        for formula in formula_powerset
    ]

    for i, f0 in enumerate(formula_powerset):
        for j, f1 in enumerate(formula_powerset[i + 1:], start=i + 1):
            for i0, c0, i1, c1 in ((i, f0, j, f1), (j, f1, i, f0)):
                if (
                    c0 not in formula_containments[i1] and
                    is_contained(c0, c1)
                ):
                    formula_containments[i0] |= (
                        {c1} | formula_containments[i1] - {c0}
                    )

    fcs = {
        formula: containment
        for formula, containment in
        zip(formula_powerset, formula_containments)
    }
    formulas_weights = mobius_weights(fcs)
    return formulas_weights


def mobius_weights(formula_containments):
    _mobius_weights = {}
    for formula in formula_containments:
        _mobius_weights[formula] = mobius_function(
            formula, formula_containments, _mobius_weights
        )
    return _mobius_weights


def mobius_function(formula, formula_containments, known_weights=None):
    if known_weights is None:
        known_weights = dict()
    if formula in known_weights:
        return known_weights[formula]
    res = -1
    for f in formula_containments[formula]:
        weight = known_weights.setdefault(
            f,
            mobius_function(
                f,
                formula_containments, known_weights=known_weights
            )
        )
        res += weight
    return -res


def powerset(iterable):
    s = list(iterable)
    return chain.from_iterable(combinations(s, r) for r in range(len(s)+1))


def symbolic_shattering(unified_query, symbol_table):
    shattered_query = shatter_easy_probfacts(unified_query, symbol_table)
    inverted_symbol_table = {v: k for k, v in symbol_table.items()}
    for atom in extract_logic_atoms(shattered_query):
        functor = atom.functor
        if isinstance(functor, ProbabilisticFactSet):
            if functor not in inverted_symbol_table:
                s = Symbol.fresh()
                inverted_symbol_table[functor] = s
                symbol_table[s] = functor

    shattered_query = (
            ReplaceExpressionWalker(inverted_symbol_table)
            .walk(shattered_query)
    )
    return shattered_query


def is_atom_a_deterministic_relation(atom, symbol_table):
    return (
        isinstance(atom.functor, Symbol)
        and atom.functor in symbol_table
        and isinstance(symbol_table[atom.functor], DeterministicFactSet)
    )


def is_atom_a_probabilistic_choice_relation(atom, symbol_table):
    return (
        isinstance(atom.functor, Symbol)
        and atom.functor in symbol_table
        and isinstance(symbol_table[atom.functor], ProbabilisticChoiceSet)
    )


def is_probabilistic_atom_with_constants_in_all_key_positions(
    atom, symbol_table
):
    """
    As we only handle probabilistic choice relations (and not more general BID
    tables), which are relations with no key attribute, there are only two
    cases:
    (1) if the atom is a probabilistic choice, then it validates the
    requirement of having constants in all key positions (it has no key
    attribute), or
    (2) if the atom is a tuple-independent relation, all its attributes are key
    attributes, and so all its terms must be constants for the requirement to
    be validated.

    """
    return is_atom_a_probabilistic_choice_relation(atom, symbol_table) or (
        not is_atom_a_deterministic_relation(atom, symbol_table)
        and (
            isinstance(atom.functor, Symbol)
            and atom.functor in symbol_table
            and isinstance(symbol_table[atom.functor], ProbabilisticFactSet)
            and all(isinstance(arg, Constant) for arg in atom.args)
        )
    )
