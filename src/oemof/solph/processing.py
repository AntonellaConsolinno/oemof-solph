# -*- coding: utf-8 -*-

"""Modules for providing a convenient data structure for solph results.

Information about the possible usage is provided within the examples.

SPDX-FileCopyrightText: Uwe Krien <krien@uni-bremen.de>
SPDX-FileCopyrightText: Simon Hilpert
SPDX-FileCopyrightText: Cord Kaldemeyer
SPDX-FileCopyrightText: Stephan Günther
SPDX-FileCopyrightText: henhuy
SPDX-FileCopyrightText: Johannes Kochems

SPDX-License-Identifier: MIT

"""

import sys
from itertools import groupby

import pandas as pd
from oemof.network.network import Node
from pyomo.core.base.piecewise import IndexedPiecewise
from pyomo.core.base.var import Var

from .helpers import flatten


def get_tuple(x):
    """Get oemof tuple within iterable or create it

    Tuples from Pyomo are of type `(n, n, int)`, `(n, n)` and `(n, int)`.
    For single nodes `n` a tuple with one object `(n,)` is created.
    """
    for i in x:
        if isinstance(i, tuple):
            return i
        elif issubclass(type(i), Node):
            return (i,)

    # for standalone variables, x is used as identifying tuple
    if isinstance(x, tuple):
        return x


def get_timestep(x):
    """Get the timestep from oemof tuples

    The timestep from tuples `(n, n, int)`, `(n, n)`, `(n, int)` and (n,)
    is fetched as the last element. For time-independent data (scalars)
    zero ist returned.
    """
    if all(issubclass(type(n), Node) for n in x):
        return 0
    else:
        return x[-1]


def remove_timestep(x):
    """Remove the timestep from oemof tuples

    The timestep is removed from tuples of type `(n, n, int)` and `(n, int)`.
    """
    if all(issubclass(type(n), Node) for n in x):
        return x
    else:
        return x[:-1]


def create_dataframe(om):
    """Create a result DataFrame with all optimization data

    Results from Pyomo are written into one common pandas.DataFrame where
    separate columns are created for the variable index e.g. for tuples
    of the flows and components or the timesteps.
    """
    # get all pyomo variables including their block
    block_vars = list(
        set([bv.parent_component() for bv in om.component_data_objects(Var)])
    )
    var_dict = {}
    for bv in block_vars:
        # Drop the auxiliary variables introduced by pyomo's Piecewise
        parent_component = bv.parent_block().parent_component()
        if not isinstance(parent_component, IndexedPiecewise):
            for i in getattr(bv, "_index"):
                key = (str(bv).split(".")[0], str(bv).split(".")[-1], i)
                value = bv[i].value
                var_dict[key] = value

    # use this to create a pandas dataframe
    df = pd.DataFrame(list(var_dict.items()), columns=["pyomo_tuple", "value"])
    df["variable_name"] = df["pyomo_tuple"].str[1]

    # adapt the dataframe by separating tuple data into columns depending
    # on which dimension the variable/parameter has (scalar/sequence).
    # columns for the oemof tuple and timestep are created
    df["oemof_tuple"] = df["pyomo_tuple"].map(get_tuple)
    df = df[df["oemof_tuple"].map(lambda x: x is not None)]
    df["timestep"] = df["oemof_tuple"].map(get_timestep)
    df["oemof_tuple"] = df["oemof_tuple"].map(remove_timestep)

    # Hack: Use another call of remove timestep to get rid of period not needed
    df.loc[df["variable_name"] == "flow", "oemof_tuple"] = df.loc[
        df["variable_name"] == "flow", "oemof_tuple"
    ].map(remove_timestep)

    # order the data by oemof tuple and timestep
    df = df.sort_values(["oemof_tuple", "timestep"], ascending=[True, True])

    # drop empty decision variables
    df = df.dropna(subset=["value"])

    return df


def results(om):
    """Create a nested result dictionary from the result DataFrame

    The already rearranged results from Pyomo from the result DataFrame are
    transferred into a nested dictionary of pandas objects.
    The first level key of that dictionary is a node (denoting the respective
    flow or component).

    The second level keys are "sequences" and "scalars" for a *standard model*:

    * A pd.DataFrame holds all results that are time-dependent, i.e. given as
      a sequence and can be indexed with the energy system's timeindex.
    * A pd.Series holds all scalar values which are applicable for timestep 0
      (i.e. investments).

    For a *multi-period model*, the second level key for "sequences" remains
    the same while instead of "scalars", the key "period_scalars" is used:

    * For sequences, see standard model.
    * Instead of a pd.Series, a pd.DataFrame holds scalar values indexed
      by periods. These hold investment-related variables.

    Examples
    --------
    * *Standard model*: `results[idx]['scalars']`
      and flows `results[n, n]['sequences']`.
    * *Multi-period model*: `results[idx]['period_scalars']`
      and flows `results[n, n]['sequences']`.
    """
    # Extraction steps that are the same for both model types
    df = create_dataframe(om)

    # create a dict of dataframes keyed by oemof tuples
    df_dict = {
        k
        if len(k) > 1
        else (k[0], None): v[["timestep", "variable_name", "value"]]
        for k, v in df.groupby("oemof_tuple")
    }

    result = {}

    # Standard model results extraction
    if not om.es.multi_period:
        result = _extract_standard_model_result(om, df_dict, result)
        scalars_col = "scalars"

    # Results extraction for a multi-period model
    else:
        period_indexed = ["invest", "total", "old", "old_end", "old_exo"]

        result = _extract_multi_period_model_result(
            om,
            df_dict,
            period_indexed,
            result,
        )
        scalars_col = "period_scalars"

    # add dual variables for bus constraints
    if om.dual is not None:
        grouped = groupby(
            sorted(om.BusBlock.balance.iterkeys()), lambda p: p[0]
        )
        for bus, timeindex in grouped:
            duals = [
                om.dual[om.BusBlock.balance[bus, p, t]]
                for _, p, t in timeindex
            ]
            df = pd.DataFrame({"duals": duals}, index=om.es.timeindex)
            if (bus, None) not in result.keys():
                result[(bus, None)] = {
                    "sequences": df,
                    scalars_col: pd.Series(dtype=float),
                }
            else:
                result[(bus, None)]["sequences"]["duals"] = duals

    return result


def _extract_standard_model_result(om, df_dict, result):
    """Extract and return the results of a standard model

    * Set index to timeindex and pivot results such that values are displayed
      for the respective variables. Reindex with the energy system's timeindex.
    * Filter for columns with nan values to retrieve scalar variables. Split
      up the DataFrame into sequences and scalars and return it.

    Parameters
    ----------
    om : oemof.solph.models.Model
        The optimization model
    df_dict : dict
        dictionary of results DataFrames
    result : dict
        dictionary to store the results

    Returns
    -------
    result : dict
        dictionary with results stored
    """
    for k in df_dict:
        df_dict[k] = _do_basic_results_extraction(om, df_dict, k)
        try:
            condition = df_dict[k].isnull().any()
            scalars = df_dict[k].loc[:, condition].dropna().iloc[0]
            sequences = df_dict[k].loc[:, ~condition]
            result[k] = {"scalars": scalars, "sequences": sequences}
        except IndexError:
            error_message = (
                "Cannot access index on result data. "
                + "Did the optimization terminate"
                + " without errors?"
            )
            raise IndexError(error_message)

    return result


def _do_basic_results_extraction(om, df_dict, k, reindex=True):
    """Do a basic iterative results extraction for node k

    Set index to timeindex and pivot results such that values are displayed
    for the respective variables. Use energy system's timeindex as index
    if reindex = True (default; used for a standard model).

    Parameters
    ----------
    om : oemof.solph.models.Model
        The optimization model
    df_dict : dict
        dictionary of results DataFrames
    k :
        oemof tuple
    reindex : boolean
        Reindex using the energy system's timeindex as an index if True

    Returns
    -------
    df_dict[k] : pd.DataFrame
        Manipulated results for node k
    """
    df_dict[k].set_index("timestep", inplace=True)
    df_dict[k] = df_dict[k].pivot(columns="variable_name", values="value")

    if reindex:
        try:
            df_dict[k].index = om.es.timeindex
        except ValueError as e:
            msg = (
                "\nFlowBlock: {0}-{1}. This could be caused by NaN-values in"
                " your input data."
            )
            raise type(e)(
                str(e) + msg.format(k[0].label, k[1].label)
            ).with_traceback(sys.exc_info()[2])

    return df_dict[k]


def _extract_multi_period_model_result(
    om,
    df_dict,
    period_indexed=None,
    result=None,
):
    """Extract and return the results of a multi-period model

    Difference to standard model is in the way, scalar values are extracted
    since they now depend on periods.

    Parameters
    ----------
    om : oemof.solph.models.Model
        The optimization model
    df_dict : dict
        dictionary of results DataFrames
    period_indexed : list
        list of variables that are indexed by periods
    result : dict
        dictionary to store the results

    Returns
    -------
    result : dict
        dictionary with results stored
    """
    for k in df_dict:
        df_dict[k] = _do_basic_results_extraction(
            om, df_dict, k, reindex=False
        )
        # Split data set
        period_cols = [
            col for col in df_dict[k].columns if col in period_indexed
        ]
        # map periods to their start years for displaying period results
        d = {
            key: val + om.es.periods[0].min().year
            for key, val in om.es.periods_years.items()
        }
        period_scalars = df_dict[k].loc[:, period_cols].dropna()
        sequences = df_dict[k].loc[
            :, [col for col in df_dict[k].columns if col not in period_cols]
        ]
        sequences.index = om.es.timeindex
        if period_scalars.empty:
            period_scalars = pd.DataFrame(index=d.values())
        try:
            period_scalars.rename(index=d, inplace=True)
            period_scalars.index.name = "period"
            result[k] = {
                "period_scalars": period_scalars,
                "sequences": sequences,
            }
        except IndexError:
            error_message = (
                "Some indices seem to be not matching.\n"
                "Cannot properly extract model results."
            )
            raise IndexError(error_message)

    return result


def convert_keys_to_strings(result, keep_none_type=False):
    """
    Convert the dictionary keys to strings.

    All (tuple) keys of the result object e.g. results[(pp1, bus1)] are
    converted into strings that represent the object labels
    e.g. results[('pp1','bus1')].
    """
    if keep_none_type:
        converted = {
            tuple([str(e) if e is not None else None for e in k])
            if isinstance(k, tuple)
            else str(k)
            if k is not None
            else None: v
            for k, v in result.items()
        }
    else:
        converted = {
            tuple(map(str, k)) if isinstance(k, tuple) else str(k): v
            for k, v in result.items()
        }
    return converted


def meta_results(om, undefined=False):
    """
    Fetch some meta data from the Solver. Feel free to add more keys.

    Valid keys of the resulting dictionary are: 'objective', 'problem',
    'solver'.

    om : oemof.solph.Model
        A solved Model.
    undefined : bool
        By default (False) only defined keys can be found in the dictionary.
        Set to True to get also the undefined keys.

    Returns
    -------
    dict
    """
    meta_res = {"objective": om.objective()}

    for k1 in ["Problem", "Solver"]:
        k1 = k1.lower()
        meta_res[k1] = {}
        for k2, v2 in om.es.results[k1][0].items():
            try:
                if str(om.es.results[k1][0][k2]) == "<undefined>":
                    if undefined:
                        meta_res[k1][k2] = str(om.es.results[k1][0][k2])
                else:
                    meta_res[k1][k2] = om.es.results[k1][0][k2]
            except TypeError:
                if undefined:
                    msg = "Cannot fetch meta results of type {0}"
                    meta_res[k1][k2] = msg.format(
                        type(om.es.results[k1][0][k2])
                    )

    return meta_res


def __separate_attrs(system, get_flows=False, exclude_none=True):
    """
    Create a dictionary with flow scalars and series.

    The dictionary is structured with flows as tuples and nested dictionaries
    holding the scalars and series e.g.
    {(node1, node2): {'scalars': {'attr1': scalar, 'attr2': 'text'},
    'sequences': {'attr1': iterable, 'attr2': iterable}}}

    om : A solved oemof.solph.Model.

    Returns
    -------
    dict
    """

    def detect_scalars_and_sequences(com):
        com_data = {"scalars": {}, "sequences": {}}

        exclusions = (
            "__",
            "_",
            "registry",
            "inputs",
            "outputs",
            "register",
            "Label",
            "from_object",
            "input",
            "output",
            "constraint_group",
        )
        attrs = [
            i
            for i in dir(com)
            if not (callable(i) or i.startswith(exclusions))
        ]

        for a in attrs:
            attr_value = getattr(com, a)

            # Iterate trough investment and add scalars and sequences with
            # "investment" prefix to component data:
            if attr_value.__class__.__name__ == "Investment":
                invest_data = detect_scalars_and_sequences(attr_value)
                com_data["scalars"].update(
                    {
                        "investment_" + str(k): v
                        for k, v in invest_data["scalars"].items()
                    }
                )
                com_data["sequences"].update(
                    {
                        "investment_" + str(k): v
                        for k, v in invest_data["sequences"].items()
                    }
                )
                continue

            if isinstance(attr_value, str):
                com_data["scalars"][a] = attr_value
                continue

            # If the label is a tuple it is iterable, therefore it should be
            # converted to a string. Otherwise it will be a sequence.
            if a == "label":
                attr_value = str(attr_value)

            # check if attribute is iterable
            # see: https://stackoverflow.com/questions/1952464/
            # in-python-how-do-i-determine-if-an-object-is-iterable
            try:
                _ = (e for e in attr_value)
                com_data["sequences"][a] = attr_value
            except TypeError:
                com_data["scalars"][a] = attr_value

        com_data["sequences"] = flatten(com_data["sequences"])
        move_undetected_scalars(com_data)
        if exclude_none:
            remove_nones(com_data)

        com_data = {
            "scalars": pd.Series(com_data["scalars"]),
            "sequences": pd.DataFrame(com_data["sequences"]),
        }
        return com_data

    def move_undetected_scalars(com):
        for ckey, value in list(com["sequences"].items()):
            if isinstance(value, str):
                com["scalars"][ckey] = value
                del com["sequences"][ckey]
                continue
            try:
                _ = (e for e in value)
            except TypeError:
                com["scalars"][ckey] = value
                del com["sequences"][ckey]
            else:
                try:
                    if not value.default_changed:
                        com["scalars"][ckey] = value.default
                        del com["sequences"][ckey]
                except AttributeError:
                    pass

    def remove_nones(com):
        for ckey, value in list(com["scalars"].items()):
            if value is None:
                del com["scalars"][ckey]
        for ckey, value in list(com["sequences"].items()):
            if len(value) == 0 or value[0] is None:
                del com["sequences"][ckey]

    # Check if system is es or om:
    if system.__class__.__name__ == "EnergySystem":
        components = system.flows() if get_flows else system.nodes
    else:
        components = system.flows if get_flows else system.es.nodes

    data = {}
    for com_key in components:
        component = components[com_key] if get_flows else com_key
        component_data = detect_scalars_and_sequences(component)
        comkey = com_key if get_flows else (com_key, None)
        data[comkey] = component_data
    return data


def parameter_as_dict(system, exclude_none=True):
    """
    Create a result dictionary containing node parameters.

    Results are written into a dictionary of pandas objects where
    a Series holds all scalar values and a dataframe all sequences for nodes
    and flows.
    The dictionary is keyed by flows (n, n) and nodes (n, None), e.g.
    `parameter[(n, n)]['sequences']` or `parameter[(n, n)]['scalars']`.

    Parameters
    ----------
    system: energy_system.EnergySystem
        A populated energy system.
    exclude_none: bool
        If True, all scalars and sequences containing None values are excluded

    Returns
    -------
    dict: Parameters for all nodes and flows
    """

    flow_data = __separate_attrs(system, True, exclude_none)
    node_data = __separate_attrs(system, False, exclude_none)

    flow_data.update(node_data)
    return flow_data
