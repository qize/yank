import os
import copy
import inspect
import logging
from datetime import date
from collections.abc import Mapping, Sequence

import cerberus
import cerberus.errors
import simtk.unit as unit
import openmmtools as mmtools
from openmoltools.utils import unwrap_py2

from .. import utils, restraints

logger = logging.getLogger(__name__)


class YANKCerberusValidator(cerberus.Validator):
    """
    Custom cerberus.Validator class for YANK extending the Validator to include all the methods needed
    by YANK
    """

    # ====================================================
    # DEFAULT SETTERS
    # ====================================================

    def _normalize_default_setter_no_parameters(self, document):
        return dict(parameters=list())

    # ====================================================
    # DATA COERCION
    # ====================================================

    def _normalize_coerce_single_str_to_list(self, value):
        """Cast a single string to a list of string"""
        return [value] if isinstance(value, str) else value

    # ====================================================
    # DATA VALIDATORS
    # ====================================================

    def _validator_file_exists(self, field, filepath):
        """Assert that the file is in fact, a file!"""
        if not os.path.isfile(filepath):
            self._error(field, 'File path {} does not exist.'.format(filepath))

    def _validator_directory_exists(self, field, directory_path):
        """Assert that the file is in fact, a file!"""
        if not os.path.isdir(directory_path):
            self._error(field, 'Directory {} does not exist.'.format(directory_path))

    def _validator_is_peptide(self, field, filepath):
        """Input file is a peptide."""
        extension = os.path.splitext(filepath)[1]
        if extension != '.pdb':
            self._error(field, "Not a .pdb file")

    def _validator_is_small_molecule(self, field, filepath):
        """Input file is a small molecule."""
        file_formats = frozenset(['mol2', 'sdf', 'smiles', 'csv'])
        extension = os.path.splitext(filepath)[1][1:]
        if extension not in file_formats:
            self._error(field, 'File is not one of {}'.format(file_formats))

    def _validator_positive_int_list(self, field, value):
        for p in value:
            if not isinstance(p, int) or not p >= 0:
                self._error(field, "{} of must be a positive integer!".format(p))

    def _validator_int_or_all_string(self, field, value):
        if value != 'all' and not isinstance(value, int):
            self._error(field, "{} must be an int or the string 'all'".format(value))

    def _validator_supported_system_files(self, field, file_paths):
        """Ensure the input system files are supported."""
        # Obtain the extension of the system files.
        file_extensions = {os.path.splitext(file_path)[1][1:] for file_path in file_paths}

        # Find a match for the extensions.
        expected_extensions = [
            ('amber', {'inpcrd', 'prmtop'}),
            ('amber', {'rst7', 'prmtop'}),
            ('gromacs', {'gro', 'top'}),
            ('openmm', {'pdb', 'xml'})
        ]
        file_extension_type = None
        for extension_type, valid_extensions in expected_extensions:
            if file_extensions == valid_extensions:
                file_extension_type = extension_type
                break

        # Verify we found a match.
        if file_extension_type is None:
            self._error(field, '{} must have file extensions matching one of the '
                               'following types: {}'.format(field, expected_extensions))
            return

        logger.debug('Correctly recognized {}files ({}) as {} '
                     'files'.format(field, file_paths, file_extension_type))

    def _validator_is_restraint_constructor(self, field, constructor_description):
        self._check_subclass_constructor(field, call_restraint_constructor, constructor_description)

    def _validator_is_mcmc_move_constructor(self, field, constructor_description):
        self._check_subclass_constructor(field, call_mcmc_move_constructor, constructor_description)

    def _validator_is_sampler_constructor(self, field, constructor_description):
        # Check if the MCMCMove is defined (its validation is done in
        # is_mcmc_move_constructor). Don't modify original dictionary.
        constructor_description = copy.deepcopy(constructor_description)
        constructor_description.pop('mcmc_moves', None)
        # Validate sampler.
        self._check_subclass_constructor(field, call_sampler_constructor, constructor_description)

    # ====================================================
    # DATA TYPES
    # ====================================================

    def _validate_type_quantity(self, value):
        if isinstance(value, unit.Quantity):
            return True

    # ====================================================
    # INTERNAL USAGE
    # ====================================================

    def _check_subclass_constructor(self, field, call_constructor_func,
                                    constructor_description):
        """Utility function for logging constructor validation errors."""
        try:
            call_constructor_func(constructor_description)
        except RuntimeError as e:
            self._error(field, str(e))


# ==============================================================================
# STATIC VALIDATORS/COERCERS
# ==============================================================================

def to_unit_coercer(compatible_units):
    """Function generator to test unit bearing strings with Cerberus."""
    def _to_unit_coercer(quantity_str):
        return utils.quantity_from_string(quantity_str, compatible_units)
    return _to_unit_coercer


def to_integer_or_infinity_coercer(value):
    """Coerce everything that is not infinity to an integer."""
    if value != float('inf'):
        value = int(value)
    return value


def to_none_int_or_checkpoint(value):
    """Coerce-ish a value that is None, int, or "checkpoint" """
    if value is None or value == "checkpoint":
        return value
    else:
        return int(value)


# ==============================================================================
# OBJECT CONSTRUCTORS PARSING
# ==============================================================================

def _check_type_keyword(constructor_description):
    """Raise RuntimeError if 'type' is not in the description"""
    subcls_name = constructor_description.get('type', None)
    if subcls_name is None:
        raise RuntimeError("'type' must be specified")
    elif not isinstance(subcls_name, str):
        raise RuntimeError("'type' must be a string")


def call_constructor(parent_cls, constructor_description, special_conversions=None,
                     convert_quantity_strings=False, **default_kwargs):
    """Convert the constructor representation into an object.

    Parameters
    ----------
    parent_cls : type
        Only objects that inherit from this class are created.
    constructor_description : dict
        Must contain the key 'type' with the name (string) of the subclass
        of ``parent_cls`` and all keyword arguments to pass to its ``__init__``
        method.
    special_conversions : dict, optional
        Special conversions to pass to ``yank.utils.validate_parameters``.
    convert_quantity_strings : bool, optional
        If True, all the string constructor values are attempted to be
        converted to quantities before creating the object (default is
        False).
    **default_kwargs
        Overwrite the default value of the constructor keyword arguments.

    Returns
    -------
    obj: parent_cls
        The new instance of the object.

    Raises
    ------
    RuntimeError
        If an error occurred parsing the constructor description of
        while creating the object.

    See Also
    --------
    yank.utils.validate_parameters

    """
    if special_conversions is None:
        special_conversions = {}
    _check_type_keyword(constructor_description)

    # Check Retrieve subclass from 'type' keyword.
    constructor_description = copy.deepcopy(constructor_description)
    subcls_name = constructor_description.pop('type', None)
    try:
        subcls = mmtools.utils.find_subclass(parent_cls, subcls_name)
    except ValueError as e:
        raise RuntimeError(str(e))

    # Read the keyword arguments of the constructor.
    constructor_kwargs = utils.get_keyword_args(subcls.__init__, try_mro_from_class=subcls)

    # Overwrite eventual keyword arguments.
    for k, v in default_kwargs.items():
        if k in constructor_kwargs and k not in constructor_description:
            constructor_description[k] = v

    # Validate init keyword arguments.
    try:
        constructor_kwargs = utils.validate_parameters(
            parameters=constructor_description, template_parameters=constructor_kwargs,
            check_unknown=True, process_units_str=True, float_to_int=True,
            special_conversions=special_conversions
        )
    except (TypeError, ValueError) as e:
        raise RuntimeError('Validation of constructor failed with: {}'.format(str(e)))

    # Convert all string quantities if required.
    if convert_quantity_strings:
        for key, value in constructor_kwargs.items():
            try:
                quantity = utils.quantity_from_string(value)
            except:
                pass
            else:
                constructor_kwargs[key] = quantity

    # Attempt to instantiate object.
    try:
        obj = subcls(**constructor_kwargs)
    except Exception as e:
        raise RuntimeError('Attempt to initialize failed with: {}'.format(str(e)))

    return obj


def call_restraint_constructor(constructor_description):
    return call_constructor(restraints.ReceptorLigandRestraint, constructor_description,
                            convert_quantity_strings=True)


def call_mcmc_move_constructor(constructor_description, **default_kwargs):
    """Create an MCMCMove from a dict representation of the constructor parameters.

    Parameters
    ----------
    constructor_description : dict
        A dict containing the keyword ``'type'`` to indicate the class
        of the MCMCMove and all the keyword arguments to pass to its
        constructor. If ``'type'`` is ``'SequenceMove'``, the ``'move_list'``
        keyword should be a list of dict representations of the single
        moves.
    **default_kwargs
        Overwrite the the default keyword argument of the MCMCMove constructor
        or, if a ``SequenceMove`` is instantiated, the constructors of the move
        list.

    Returns
    -------
    mcmc_move : openmmtools.mcmc.MCMCMove
        The intantiation of the ``MCMCMove`` object.
    """
    # Check if this is a sequence of moves and we need to unnest the constructors.
    # If we end up with other nested constructors, we'll probably need a general strategy.
    _check_type_keyword(constructor_description)
    is_sequence_move = constructor_description['type'] == 'SequenceMove'
    if is_sequence_move and 'move_list' not in constructor_description:
        raise RuntimeError('A SequenceMove must specify a "move_list" keyword '
                           'containing a list of MCMCMoves')
    # Prepare nested MCMCMoves.
    if is_sequence_move:
        mcmc_moves_constructors = constructor_description['move_list']
    else:
        mcmc_moves_constructors = [constructor_description]
    # Call constructor of all moves.
    moves = []
    for mcmc_move_constructor in mcmc_moves_constructors:
        moves.append(call_constructor(mmtools.mcmc.MCMCMove, mcmc_move_constructor,
                                      convert_quantity_strings=True, **default_kwargs))
    if is_sequence_move:
        return mmtools.mcmc.SequenceMove(move_list=moves)
    return moves[0]


def call_sampler_constructor(constructor_description):
    special_conversions = {'number_of_iterations': to_integer_or_infinity_coercer,
                           'online_analysis_interval': to_none_int_or_checkpoint}
    return call_constructor(mmtools.multistate.MultiStateSampler, constructor_description,
                            special_conversions=special_conversions)


# ==============================================================================
# AUTOMATIC SCHEMA GENERATION
# ==============================================================================

def generate_unknown_type_validator(a_type):
    """
    Helper function to :func:`type_to_cerberus_map` to convert unknown types into a validator

    Parameters
    ----------
    a_type : type
        Any valid type, although this works for any type, its preferred to let :func:`type_to_cerberus_map`
        to invoke this method

    Returns
    -------
    validator_map : dict
        Map dictionary of form ``{'validator': <function>}`` where ``'validator'`` is literal and
        ``<function>`` is a validator function of type checker.
    """
    def nonstandard_type_validator(field, value, error):
        if type(value) is not a_type:
            error(field, 'Must be of type {}'.format(mmtools.utils.typename(a_type)))
    validator_dict = {'validator': nonstandard_type_validator}
    return validator_dict


def type_to_cerberus_map(a_type):
    """
    Convert a provided type to a valid Cerberus internal type dict.

    Parameters
    ----------
    a_type : type
        Type you want the converter to built-in Cerberus strings

    Returns
    -------
    type_map : dict
        Type map dictionary of form ``{'type': 'TYPE'}`` where ``'type'`` is literal and ``TYPE`` is the mapped
        type in Cerberus

        OR

         Map dictionary of form ``{'validator': <function>}`` where ``'validator'`` is literal and
        ``<function>`` is a validator function of type checker for non-standard

    Raises
    ------
    YANKCerberusUtilsError
        When the type has no known map
    """
    known_types = {bool: 'boolean',
                   bytes: 'binary',
                   bytearray: 'binary',
                   date: 'date',
                   Mapping: 'dict',
                   dict: 'dict',
                   float: 'float',
                   int: 'integer',
                   tuple: 'list',
                   list: 'list',
                   Sequence: 'list',
                   str: 'string',
                   set: 'set'}
    try:
        type_map = {'type': known_types[a_type]}
    except KeyError:
        type_map = generate_unknown_type_validator(a_type)
    return type_map


def generate_signature_schema(func, update_keys=None, exclude_keys=frozenset()):
    """Generate a dictionary to test function signatures with Cerberus' Schema.

    Parameters
    ----------
    func : function
        The function used to build the schema.
    update_keys : dict
        Keys in here have priority over automatic generation. It can be
        used to make an argument mandatory, or to use a specific validator.
    exclude_keys : list-like
        Keys in here are ignored and not included in the schema.

    Returns
    -------
    func_schema : dict
        The dictionary to be used as Cerberus Validator schema. Contains all keyword
        variables in the function signature as optional argument with
        the default type as validator. Unit bearing strings are converted.
        Argument with default None are always accepted. Camel case
        parameters in the function are converted to underscore style.

    Examples
    --------
    >>> from cerberus import Validator
    >>> def f(a, b, camelCase=True, none=None, quantity=3.0*unit.angstroms):
    ...     pass
    >>> f_dict = generate_signature_schema(f, exclude_keys=['quantity'])
    >>> print(isinstance(f_dict, dict))
    True
    >>> f_validator = Validator(generate_signature_schema(f))
    >>> f_validator.validated({'quantity': '1.0*nanometer'})
    {'quantity': Quantity(value=1.0, unit=nanometer)}

    """
    if update_keys is None:
        update_keys = {}

    func_schema = {}
    arg_spec = inspect.getfullargspec(unwrap_py2(func))
    args = arg_spec.args
    defaults = arg_spec.defaults

    # Check keys that must be excluded from first pass
    exclude_keys = set(exclude_keys)
    exclude_keys.update(update_keys)

    # Transform camelCase to underscore
    args = [utils.camelcase_to_underscore(arg) for arg in args]

    # Build schema
    optional_validator = {'required': False}  # Keys are always optional for this type
    for arg, default_value in zip(args[-len(defaults):], defaults):
        if arg not in exclude_keys:  # User defined keys are added later
            if default_value is None:  # None defaults are always accepted, and considered nullable
                validator = {'nullable': True}
            elif isinstance(default_value, unit.Quantity):  # Convert unit strings
                validator = {'coerce': to_unit_coercer(default_value.unit)}
            else:
                validator = type_to_cerberus_map(type(default_value))
            # Add the argument to the existing schema as a keyword
            # To the new keyword, add the optional flag and the "validator" flag
            # of either 'validator' or 'type' depending on how it was processed
            func_schema = {**func_schema, **{arg: {**optional_validator, **validator}}}

    # Add special user keys
    func_schema.update(update_keys)

    return func_schema

