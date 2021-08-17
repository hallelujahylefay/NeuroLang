"""
Implentation of probabilistic query resolution for
hierarchical queries [^1]. Using this we apply the small dichotomy
theorem [^1, ^2]:
Let Q be a conjunctive query without self-joins or a non-repeating relational
algebra expression. Then:
* If Q is hierarchical, then P(Q) is in polynomial time, and can be computed
using only the lifted inference rules for join, negation, union, and
existential quantifier.

* If Q is not hierarchical, then P(Q) is #P-hard in the size of the database.

[^1]: Robert Fink and Dan Olteanu. A dichotomy for non-repeating queries with
negation in probabilistic databases. In Proceedings of the 33rd ACM
SIGMOD-SIGACT-SIGART Symposium on Principles of Database Systems, PODS ’14,
pages 144–155, New York, NY, USA, 2014. ACM.

[^2]: Nilesh N. Dalvi and Dan Suciu. Efficient query evaluation on
probabilistic databases. VLDB J., 16(4):523–544, 2007.
"""

import logging
from collections import defaultdict

from ..datalog.expression_processing import (
    EQ,
    UnifyVariableEqualities,
    extract_logic_atoms,
    flatten_query,
    remove_conjunction_duplicates,
)
from ..datalog.translate_to_named_ra import TranslateToNamedRA
from ..exceptions import UnsupportedSolverError
from ..expression_walker import ExpressionWalker
from ..expressions import Symbol
from ..logic import FALSE, Conjunction, Disjunction, Implication, Negation
from ..logic.transformations import GuaranteeConjunction
from ..relational_algebra import (
    ColumnStr,
    EliminateTrivialProjections,
    NamedRelationalAlgebraFrozenSet,
    Projection,
    RelationalAlgebraPushInSelections,
    str2columnstr_constant,
)
from ..utils import log_performance
from ..utils.orderedset import OrderedSet
from .exceptions import NotHierarchicalQueryException
from .expression_processing import lift_optimization_for_choice_predicates
from .probabilistic_ra_utils import (
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

LOG = logging.getLogger(__name__)


def is_hierarchical_without_self_joins(query):
    """
    Let Q be first-order formula. For each variable x denote at(x) the
    set of atoms that contain the variable x. We say that Q is hierarchical
    if forall x, y one of the following holds:
    at(x) ⊆ at(y) or at(x) ⊇ at(y) or at(x) ∩ at(y) = ∅.
    """

    has_self_joins, atom_set = extract_atom_sets_and_detect_self_joins(query)

    if has_self_joins:
        return False

    variables = list(atom_set)
    for i, v in enumerate(variables):
        at_v = atom_set[v]
        for v2 in variables[i + 1:]:
            at_v2 = atom_set[v2]
            if not (at_v <= at_v2 or at_v2 <= at_v or at_v.isdisjoint(at_v2)):
                LOG.info(
                    "Not hierarchical on variables %s %s", v.name, v2.name
                )
                return False

    return True


def extract_atom_sets_and_detect_self_joins(query):
    has_self_joins = False
    predicates = extract_logic_atoms(query)
    predicates = set(pred for pred in predicates if not pred.functor == EQ)
    seen_predicate_functor = set()
    atom_set = defaultdict(set)
    for predicate in predicates:
        functor = predicate.functor
        if functor in seen_predicate_functor:
            LOG.info("Not hierarchical self join on variables %s", functor)
            has_self_joins = True
        seen_predicate_functor.add(functor)
        for variable in predicate.args:
            if not isinstance(variable, Symbol):
                continue
            atom_set[variable].add(functor)
    return has_self_joins, atom_set


class RAQueryOptimiser(
    EliminateTrivialProjections,
    RelationalAlgebraPushInSelections,
    ExpressionWalker,
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

    if isinstance(flat_query_body, Conjunction):
        flat_query_body = remove_conjunction_duplicates(flat_query_body)

    with log_performance(LOG, "Translation and lifted optimisation"):
        flat_query_body = GuaranteeConjunction().walk(
            lift_optimization_for_choice_predicates(
                flat_query_body, cpl_program
            )
        )
        flat_query = Implication(query.consequent, flat_query_body)
        symbol_table = generate_probabilistic_symbol_table_for_query(
            cpl_program, flat_query_body
        )
        unified_query = UnifyVariableEqualities().walk(flat_query)
        shattered_query = shatter_easy_probfacts(unified_query, symbol_table)
        shattered_query_probabilistic_body = Conjunction(
            tuple(
                atom
                for atom in extract_logic_atoms(shattered_query.antecedent)
                if isinstance(
                    atom.functor,
                    (ProbabilisticChoiceSet, ProbabilisticFactSet),
                )
            )
        )
        if (
            isinstance(shattered_query.antecedent, Disjunction)
            or
            _get_existential_variables_in_atoms(
                shattered_query, ProbabilisticFactSet
            )
            - _get_existential_variables_in_atoms(
                shattered_query, ProbabilisticChoiceSet
            )
        ):
            raise UnsupportedSolverError(
                "Cannot solve queries with probabilstic choices"
            )
        if not is_hierarchical_without_self_joins(
            shattered_query_probabilistic_body
        ):
            LOG.info(
                "Query with conjunctions %s not hierarchical",
                shattered_query_probabilistic_body.formulas,
            )
            raise NotHierarchicalQueryException(
                "Query not hierarchical, algorithm can't be applied"
            )
        ra_query = TranslateToNamedRA().walk(shattered_query.antecedent)
        # project on query's head variables
        ra_query = _project_on_query_head(ra_query, shattered_query)
        # re-introduce head variables potentially removed by unification
        ra_query = reintroduce_unified_head_terms(
            ra_query, flat_query, unified_query
        )
        ra_query = RAQueryOptimiser().walk(ra_query)

    with log_performance(LOG, "Run RAP query"):
        solver = ProbSemiringSolver(symbol_table)
        prob_set_result = solver.walk(ra_query)

    return prob_set_result


def _project_on_query_head(provset, query):
    proj_cols = tuple(
        OrderedSet(
            str2columnstr_constant(arg.name)
            for arg in query.consequent.args
            if isinstance(arg, Symbol)
        )
    )
    return Projection(provset, proj_cols)


def solve_marg_query(rule, cpl):
    """
    Solve a MARG query on a CP-Logic program.
    Parameters
    ----------
    query : Implication
        Consequent must be of type `Condition`.
        MARG query of the form `ans(x) :- P(x)`.
    cpl_program : CPLogicProgram
        CP-Logic program on which the query should be solved.
    Returns
    -------
    ProvenanceAlgebraSet
        Provenance set labelled with probabilities for each tuple in the result
        set.
    """
    return lift_solve_marg_query(rule, cpl, solve_succ_query)


def _get_existential_variables_in_atoms(query, functor_type):
    eq_vars = set()
    head_vars = set(
        arg for arg in query.consequent.args if isinstance(arg, Symbol)
    )
    if isinstance(query.antecedent, Conjunction):
        conjuncts = query.antecedent.formulas
    else:
        conjuncts = (query.antecedent,)
    for conjunct in conjuncts:
        literal = (
            conjunct.formula if isinstance(conjunct, Negation) else conjunct
        )
        if isinstance(literal.functor, functor_type):
            eq_vars |= set(
                arg
                for arg in literal.args
                if isinstance(arg, Symbol) and arg not in head_vars
            )
    return eq_vars
