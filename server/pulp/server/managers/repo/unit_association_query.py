# -*- coding: utf-8 -*-
#
# Copyright © 2012 Red Hat, Inc.
#
# This software is licensed to you under the GNU General Public
# License as published by the Free Software Foundation; either version
# 2 of the License (GPLv2) or (at your option) any later version.
# There is NO WARRANTY for this software, express or implied,
# including the implied warranties of MERCHANTABILITY,
# NON-INFRINGEMENT, or FITNESS FOR A PARTICULAR PURPOSE. You should
# have received a copy of GPLv2 along with this software; if not, see
# http://www.gnu.org/licenses/old-licenses/gpl-2.0.txt.

"""
Contains the manager class for performing queries for repo-unit associations.
"""

import itertools
import logging

import pymongo

from pulp.common.odict import OrderedDict
from pulp.plugins.types import database as types_db
from pulp.server.db.model.criteria import UnitAssociationCriteria
from pulp.server.db.model.repository import RepoContentUnit

# -- constants ----------------------------------------------------------------

_LOG = logging.getLogger(__name__)

# Valid sort strings
SORT_TYPE_ID = 'type_id'
SORT_OWNER_TYPE = 'owner_type'
SORT_OWNER_ID = 'owner_id'
SORT_CREATED = 'created'
SORT_UPDATED = 'updated'

_VALID_SORTS = (SORT_TYPE_ID, SORT_OWNER_TYPE, SORT_OWNER_ID, SORT_CREATED, SORT_UPDATED)

SORT_ASCENDING = pymongo.ASCENDING
SORT_DESCENDING = pymongo.DESCENDING

_VALID_DIRECTIONS = (SORT_ASCENDING, SORT_DESCENDING)

# -- manager ------------------------------------------------------------------

class RepoUnitAssociationQueryManager(object):

    def get_unit_ids(self, repo_id, unit_type_id=None):
        """
        Get the ids of the content units associated with the repo. If more
        than one association exists between a unit and the repository, the
        unit ID will only be listed once.

        DEPRECATED: the get_units calls should be used, limiting the returned
          fields to just the IDs.

        @param repo_id: identifies the repo
        @type  repo_id: str

        @param unit_type_id: optional; if specified only unit ids of the
                             specified type are returned

        @return: dict of unit type id: list of content unit ids
        @rtype:  dict of str: list of str
        """
        unit_ids = {}
        collection = RepoContentUnit.get_collection()

        # This used to be one query and splitting out the results by unit
        # type in memory. The problem is that we need to add in the distinct
        # clause to eliminate the potential of multiple associations to the
        # same unit. I don't think distinct will operate on two keys. I don't
        # anticipate there will be a tremendous amount of unit types passed in
        # so I'm not too worried about making one call per unit type.
        # jdob - Dec 9, 2011

        if unit_type_id is None:
            unit_type_ids = []

            # Get a list of all unit types that have at least one unit associated.
            cursor = collection.find(spec={'repo_id' : repo_id}, fields=['unit_type_id'])
            for t in cursor.distinct('unit_type_id'):
                unit_type_ids.append(t)
        else:
            unit_type_ids = [unit_type_id]

        for type_id in unit_type_ids:

            spec_doc = {'repo_id': repo_id,
                        'unit_type_id' : type_id}
            cursor = collection.find(spec_doc)

            for unit_id in cursor.distinct('unit_id'):
                ids = unit_ids.setdefault(type_id, [])
                ids.append(unit_id)

        return unit_ids

    @staticmethod
    def find_by_criteria(criteria):
        """
        Return a list of RepoContentUnits that match the provided criteria.

        @param criteria:    A Criteria object representing a search you want
                            to perform
        @type  criteria:    pulp.server.db.model.criteria.Criteria

        @return:    list of RepoContentUnits
        @rtype:     list
        """
        return RepoContentUnit.get_collection().query(criteria)


    def get_units(self, repo_id, criteria=None, as_generator=False):
        """
        Get the units associated with the repository based on the provided unit
        association criteria.

        :param repo_id: identifies the repository
        :type  repo_id: str

        :param criteria: if specified will drive the query
        :type  criteria: UnitAssociationCriteria

        :param as_generator: if true, return a generator; if false, a list
        :type  as_generator: bool
        """

        criteria = criteria or UnitAssociationCriteria()

        unit_associations_generator = self._unit_associations_cursor(repo_id, criteria)

        if criteria.remove_duplicates:
            unit_associations_generator = self._unit_associations_no_duplicates(unit_associations_generator)

        unit_associations_by_id = OrderedDict((u['unit_id'], u) for u in unit_associations_generator)

        unit_type_ids = criteria.type_ids or self._unit_type_ids_for_repo(repo_id)
        unit_type_ids = sorted(unit_type_ids)

        units_generator = itertools.chain(self._associated_units_by_type_cursor(unit_type_id, criteria, unit_associations_by_id.keys())
                                          for unit_type_id in unit_type_ids)

        units_generator = self._with_skip_and_limit(units_generator, criteria.skip, criteria.limit)

        if criteria.association_sort is not None:
            units_generator = self._association_ordered_units(unit_associations_by_id.keys(), units_generator)

        units_generator = self._merged_units(unit_associations_by_id, units_generator)

        if as_generator:
            return units_generator

        return list(units_generator)

    def get_units_across_types(self, repo_id, criteria=None, as_generator=False):
        """
        Retrieves data describing units associated with the given repository
        along with information on the association itself.

        As this call may span multiple unit types, sort fields are
        restricted to those related to the association itself:
        - Type ID
        - First Associated
        - Last Updated
        - Owner Type
        - Owner ID

        Multiple sort fields from the above list are supported. If no sort is
        provided, units will be sorted by unit_type_id and created (in order).

        :param repo_id: identifies the repository
        :type  repo_id: str

        :param criteria: if specified will drive the query
        :type  criteria: UnitAssociationCriteria

        :param as_generator: if true, return a generator; if false, a list
        :type  as_generator: bool
        """

        return self.get_units(repo_id, criteria, as_generator)

    def get_units_by_type(self, repo_id, type_id, criteria=None, as_generator=False):
        """
        Retrieves data describing units of the given type associated with the
        given repository. Information on the associations themselves is also
        provided.

        The sort fields may be from either the association data OR the
        unit fields. A mix of both is not supported. Multiple sort fields
        are supported as long as they come from the same area.

        If a sort is not provided, the units will be sorted ascending by each
        value in the unit key for the given type.

        :param repo_id: identifies the repository
        :type  repo_id: str

        :param type_id: limits returned units to the given type
        :type  type_id: str

        :param criteria: if specified will drive the query
        :type  criteria: UnitAssociationCriteria

        :param as_generator: if true, return a generator; if false, a list
        :type  as_generator: bool
        """

        criteria = criteria or UnitAssociationCriteria()
        # we're just going to overwrite the provided type_ids if the user was
        # dumb enough to provided them in this call
        criteria.type_ids = [type_id]

        return self.get_units(repo_id, criteria, as_generator)

    # -- unit association methods ----------------------------------------------

    @staticmethod
    def _unit_type_ids_for_repo(repo_id):
        """
        Retrieve a list of all unit type ids currently associated with the
        repository

        :type repo_id: str
        :rtype: list
        """

        collection = RepoContentUnit.get_collection()

        unit_associations = collection.find({'repo_id': repo_id}, fields=['unit_type_id'])
        unit_associations.distinct('unit_type_id')

        return [u['unit_type_id'] for u in unit_associations]

    @staticmethod
    def _unit_associations_cursor(repo_id, criteria):
        """
        Retrieve a pymongo cursor for unit associations for the given repository
        that match the given criteria.

        :type repo_id: str
        :type criteria: UnitAssociationCriteria
        :rtype: pymongo.cursor.Cursor
        """

        spec = criteria.association_filters.copy()
        spec['repo_id'] = repo_id

        if criteria.type_ids:
            spec['unit_type_id'] = {'$in': criteria.type_ids}

        collection = RepoContentUnit.get_collection()

        cursor = collection.find(spec, fields=criteria.association_fields)

        sort = criteria.association_sort or []

        # sorting by the "created" flag is crucial to removing duplicate associations
        created_sort_tuple = ('created', SORT_ASCENDING)
        if created_sort_tuple not in sort:
            sort.insert(0, created_sort_tuple)

        cursor.sort(sort)

        return cursor

    @staticmethod
    def _unit_associations_no_duplicates(iterator):
        """
        Remove duplicate unit associations from a iterator of unit associations.

        :type iterator: iterable
        :rtype: generator
        """

        # this algorithm returns the earliest association in the case of duplicates
        # this algorithm assumes the iterator is already sorted by "created"

        previously_generated_association_ids = set()

        for unit_association in iterator:

            association_id = '+'.join((unit_association['unit_type_id'], unit_association['unit_id']))

            if association_id in previously_generated_association_ids:
                continue

            yield unit_association

            previously_generated_association_ids.add(association_id)

    # -- associated units methods ----------------------------------------------

    @staticmethod
    def _associated_units_by_type_cursor(unit_type_id, criteria, associated_unit_ids):
        """
        Retrieve a pymongo cursor for units associated with a repository of a
        give unit type that meet to the provided criteria.

        :type unit_type_id: str
        :type criteria: UnitAssociationCriteria
        :type associated_unit_ids: list
        :rtype: pymongo.cursor.Cursor
        """

        collection = types_db.type_units_collection(unit_type_id)

        spec = criteria.unit_filters.copy()
        spec['_id'] = {'$in': associated_unit_ids}

        cursor = collection.find(spec, fields=criteria.unit_fields)

        sort = criteria.unit_sort or [(u, SORT_ASCENDING) for u in types_db.type_units_unit_key(unit_type_id)]
        cursor.sort(sort)

        return cursor

    @staticmethod
    def _with_skip_and_limit(iterator, skip, limit):
        """
        Skip the first *n* elements in an iterator and limit the return to *m*
        elements.

        The skip and limit arguments must either be None or a non-negative integer.

        :type iterator: iterable
        :type skip: int or None
        :type limit: int or None
        :rtype: generator
        """
        assert (isinstance(skip, int) and skip >= 0) or skip is None
        assert (isinstance(limit, int) and limit >= 0) or limit is None

        generated_elements = 0
        skipped_elements = 0

        for element in iterator:

            if limit and generated_elements - skipped_elements == limit:
                raise StopIteration()

            if skip and skipped_elements < skip:
                skipped_elements += 1
                continue

            yield element

            generated_elements += 1

    @staticmethod
    def _association_ordered_units(associated_unit_ids, associated_units):
        """
        Return associated units in the order specified by the associated unit id
        list.

        :type associated_unit_ids: list
        :type associated_units: iterator
        :rtype: generator
        """

        # this algorithm assumes that associated_unit_ids has already been sorted

        # XXX this is unfortunate as it's the one place that loads all of the
        # associated_units into memory
        associated_units_by_id = dict((u['_id'], u) for u in associated_units)

        for unit_id in associated_unit_ids:
            yield associated_units_by_id[unit_id]

    @staticmethod
    def _merged_units(unit_associations_by_id, associated_units):
        """
        Return associated units as the unit association information and the unit
        information as metadata on the unit association information.

        :type unit_associations_by_id: dict
        :type associated_units: iterator
        :rtype: generator
        """

        for unit in associated_units:
            association = unit_associations_by_id[unit['_id']]
            association['metadata'] = unit

            yield association

