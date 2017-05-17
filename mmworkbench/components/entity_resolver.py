# -*- coding: utf-8 -*-
"""
This module contains the entity resolver component of the Workbench natural language processor.
"""
from __future__ import unicode_literals
from builtins import object

import logging
import os
import copy

from ..core import Entity

from elasticsearch import Elasticsearch
from elasticsearch.helpers import streaming_bulk

logger = logging.getLogger(__name__)

DOC_TYPE = "document"
ES_SYNONYM_INDEX_PREFIX = "synonym"


class EntityResolver(object):
    """An entity resolver is used to resolve entities in a given query to their canonical values
    (usually linked to specific entries in a knowledge base).
    """

    # default ElasticSearch mapping to define text analysis settings for text fields
    DEFAULT_SYN_ES_MAPPING = {
        "mappings": {
            "document": {
                "properties": {
                    "cname": {
                        "type": "text",
                        "fields": {
                            "raw": {
                                "type": "keyword",
                                "ignore_above": 256
                            },
                            "normalized_keyword": {
                                "type": "text",
                                "analyzer": "keyword_match_analyzer"
                            },
                            "char_ngram": {
                                "type": "text",
                                "analyzer": "char_ngram_analyzer"
                            }
                        },
                        "analyzer": "default_analyzer"
                    },
                    "id": {
                        "type": "keyword"
                    },
                    "whitelist": {
                        "type": "text",
                        "fields": {
                            "raw": {
                                "type": "keyword",
                                "ignore_above": 256
                            },
                            "normalized_keyword": {
                                "type": "text",
                                "analyzer": "keyword_match_analyzer"
                            },
                            "char_ngram": {
                                "type": "text",
                                "analyzer": "char_ngram_analyzer"
                            }
                        },
                        "analyzer": "default_analyzer"
                    }
                }
            }
        },
        "settings": {
            "analysis": {
                "filter": {
                    "token_shingle": {
                        "max_shingle_size": "4",
                        "min_shingle_size": "2",
                        "output_unigrams": "true",
                        "type": "shingle"
                    },
                    "autocomplete_filter": {
                        "type": "edge_ngram",
                        "min_gram": "4",
                        "max_gram": "20"
                    }
                },
                "analyzer": {
                    "default_analyzer": {
                        "filter": [
                            "lowercase",
                            "asciifolding",
                            "token_shingle"
                        ],
                        "char_filter": [
                            "remove_comma",
                            "remove_tm_and_r",
                            "remove_loose_apostrophes",
                            "space_possessive_apostrophes",
                            "remove_special_beginning",
                            "remove_special_end",
                            "remove_special1",
                            "remove_special2",
                            "remove_special3"
                        ],
                        "type": "custom",
                        "tokenizer": "whitespace"
                    },
                    "keyword_match_analyzer": {
                        "filter": [
                            "lowercase",
                            "asciifolding"
                        ],
                        "char_filter": [
                            "remove_comma",
                            "remove_tm_and_r",
                            "remove_loose_apostrophes",
                            "space_possessive_apostrophes",
                            "remove_special_beginning",
                            "remove_special_end",
                            "remove_special1",
                            "remove_special2",
                            "remove_special3"
                        ],
                        "type": "custom",
                        "tokenizer": "keyword"
                    },
                    "char_ngram_analyzer": {
                        "filter": [
                            "lowercase",
                            "asciifolding",
                            "autocomplete_filter"
                        ],
                        "char_filter": [
                            "remove_comma",
                            "remove_tm_and_r",
                            "remove_loose_apostrophes",
                            "space_possessive_apostrophes",
                            "remove_special_beginning",
                            "remove_special_end",
                            "remove_special1",
                            "remove_special2",
                            "remove_special3"
                        ],
                        "type": "custom",
                        "tokenizer": "whitespace"
                    }
                },
                "char_filter": {
                    "remove_comma": {
                        "pattern": ",",
                        "type": "pattern_replace",
                        "replacement": ""
                    },
                    "remove_loose_apostrophes": {
                        "pattern": " '|' ",
                        "type": "pattern_replace",
                        "replacement": ""
                    },
                    "remove_special2": {
                        "pattern": "([\\p{N}]+)[^\\p{L}\\p{N}&']+(?=[\\p{L}\\s]+)",
                        "type": "pattern_replace",
                        "replacement": "$1 "
                    },
                    "remove_tm_and_r": {
                        "pattern": "™|®",
                        "type": "pattern_replace",
                        "replacement": ""
                    },
                    "remove_special3": {
                        "pattern": "([\\p{L}]+)[^\\p{L}\\p{N}&']+(?=[\\p{L}]+)",
                        "type": "pattern_replace",
                        "replacement": "$1 "
                    },
                    "remove_special1": {
                        "pattern": "([\\p{L}]+)[^\\p{L}\\p{N}&']+(?=[\\p{N}\\s]+)",
                        "type": "pattern_replace",
                        "replacement": "$1 "
                    },
                    "remove_special_end": {
                        "pattern": "[^\\p{L}\\p{N}&']+$",
                        "type": "pattern_replace",
                        "replacement": ""
                    },
                    "space_possessive_apostrophes": {
                        "pattern": "([^\\p{N}\\s]+)'s ",
                        "type": "pattern_replace",
                        "replacement": "$1 's "
                    },
                    "remove_special_beginning": {
                        "pattern": "^[^\\p{L}\\p{N}\\p{Sc}&']+",
                        "type": "pattern_replace",
                        "replacement": ""
                    }
                }
            }
        }
    }

    def __init__(self, resource_loader, entity_type):
        """Initializes an entity resolver

        Args:
            resource_loader (ResourceLoader): An object which can load resources for the resolver
            entity_type: The entity type associated with this entity resolver
        """
        self._resource_loader = resource_loader
        self._normalizer = resource_loader.query_factory.normalize
        self.type = entity_type

        self._mapping = None
        self._is_system_entity = Entity.is_system_entity(self.type)
        self.__es_client = None
        self._es_index_name = EntityResolver.ES_SYNONYM_INDEX_PREFIX + "_" + entity_type

    @property
    def _es_client(self):
        # Lazily connect to Elasticsearch
        if self.__es_client is None:
            self.__es_client = self._create_es_client()
        return self.__es_client

    @staticmethod
    def _create_es_client(es_host=None, es_user=None, es_pass=None):
        es_host = es_host or os.environ.get('MM_ES_HOST')
        es_user = es_user or os.environ.get('MM_ES_USERNAME')
        es_pass = es_pass or os.environ.get('MM_ES_PASSWORD')

        http_auth = (es_user, es_pass) if es_user and es_pass else None
        es_client = Elasticsearch(es_host, http_auth=http_auth, request_timeout=60, timeout=60)
        return es_client

    @classmethod
    def create_index(cls, index_name, es_client=None):
        """Creates a new index in the knowledge base.

        Args:
            index_name (str): The name of the new index to be created
            es_host (str): The Elasticsearch host server
            es_client: Description
        """
        es_client = es_client or cls._create_es_client()

        mapping = EntityResolver.DEFAULT_SYNC_ES_MAPPING

        if not es_client.indices.exists(index=index_name):
            logger.info("Creating index '{}'".format(index_name))
            es_client.indices.create(index_name, body=mapping)
        else:
            logger.error("Index '{}' already exists.".format(index_name))

    @classmethod
    def ingest_synonym(cls, index_name, mapping, es_client=None):
        """Loads documents from disk into the specified index in the knowledge base. If an index
        with the specified name doesn't exist, a new index with that name will be created in the
        knowledge base.

        Args:
            index_name (str): The name of the new index to be created
            data_file (str): The path to the data file containing the documents to be imported
                into the knowledge base index
            es_host (str): The Elasticsearch host server
            es_client: Description
        """
        es_client = es_client or cls._create_es_client()

        # with open(data_file) as data_fp:
        #     data = json.load(data_fp)

        def _doc_generator(docs):
            for doc in docs:
                base = {'_id': doc['id']}
                base.update(doc)
                yield base

        # create index if specified index does not exist
        if not es_client.indices.exists(index=index_name):
            EntityResolver.create_index(index_name, es_client=es_client)

        count = 0
        for okay, result in streaming_bulk(es_client, _doc_generator(mapping), index=index_name,
                                           doc_type=DOC_TYPE, chunk_size=50):

            action, result = result.popitem()
            doc_id = '/%s/%s/%s' % (index_name, DOC_TYPE, result['_id'])
            # process the information from ES whether the document has been
            # successfully indexed
            if not okay:
                logger.error('Failed to %s document %s: %r', action, doc_id, result)
            else:
                count += 1
                logger.debug('Loaded document: %s', doc_id)
        logger.info('Loaded %s document%s', count, '' if count == 1 else 's')

    # @staticmethod
    # def process_mapping(entity_type, mapping, normalizer):
    #     """
    #     Description
    #
    #     Args:
    #         entity_type: The entity type associated with this entity resolver
    #         mapping: Description
    #         normalizer: Description
    #     """
    #     item_map = {}
    #     syn_map = {}
    #     seen_ids = []
    #     for item in mapping:
    #         cname = item['cname']
    #         item_id = item.get('id')
    #         if cname in item_map:
    #             msg = 'Canonical name {!r} specified in {!r} entity map multiple times'
    #             logger.debug(msg.format(cname, entity_type))
    #         if item_id:
    #             if item_id in seen_ids:
    #                 msg = 'Item id {!r} specified in {!r} entity map multiple times'
    #                 raise ValueError(msg.format(item_id, entity_type))
    #             seen_ids.append(item_id)
    #
    #         aliases = [cname] + item.pop('whitelist', [])
    #         items_for_cname = item_map.get(cname, [])
    #         items_for_cname.append(item)
    #         item_map[cname] = items_for_cname
    #         for alias in aliases:
    #             norm_alias = normalizer(alias)
    #             if norm_alias in syn_map:
    #                 msg = 'Synonym {!r} specified in {!r} entity map multiple times'
    #                 logger.debug(msg.format(cname, entity_type))
    #             cnames_for_syn = syn_map.get(norm_alias, [])
    #             cnames_for_syn.append(cname)
    #             syn_map[norm_alias] = list(set(cnames_for_syn))
    #
    #     return {'items': item_map, 'synonyms': syn_map}

    def fit(self, clean=False):
        """Loads an entity mapping file (if one exists) or trains a machine-learned entity
        resolution model using the provided training examples

        Args:
            clean (bool): If True, deletes and recreates the index from scratch instead of
                          updating the existing index with synonyms in the mapping.json
        """
        if not self._is_system_entity:
            mapping = self._resource_loader.get_entity_map(self.type)
            # self._mapping = self.process_mapping(self.type, mapping, self._normalizer)

            # create index if specified index does not exist
            # TODO: refactor things around ES calls.
            if not self._es_client.indices.exists(index=self._es_index_name):
                EntityResolver.create_index(self._es_index_name, es_client=self._es_client)

            logger.info("Importing synonym data to ES index '{}'".format(self._es_index_name))
            EntityResolver.ingest_synonym(self._es_index_name, mapping)

    def predict(self, entity, exact_match_only=False):
        """Predicts the resolved value(s) for the given entity using the loaded entity map or the
        trained entity resolution model

        Args:
            entity (Entity): An entity found in an input query

        Returns:
            The resolved value for the provided entity
        """
        if self._is_system_entity:
            # system entities are already resolved
            return entity.value

        # TODO: revisit the normalization behavior
        normed = self._normalizer(entity.text)

        if exact_match_only:
            try:
                cnames = self._mapping['synonyms'][normed]
            except KeyError:
                logger.warning('Failed to resolve entity %r for type %r', entity.text, entity.type)
                return entity.text

            if len(cnames) > 1:
                logger.info('Multiple possible canonical names for %r entity for type %r',
                            entity.text, entity.type)

            values = []
            for cname in cnames:
                for item in self._mapping['items'][cname]:
                    item_value = copy.copy(item)
                    item_value.pop('whitelist', None)
                    values.append(item_value)

            return values

        full_text_query = {
            "query": {
                "bool": {
                    "should": [
                        {
                            "bool": {
                                "should": [
                                    {
                                        "match": {
                                            "whitelist.normalized_keyword": {
                                                "query": normed,
                                                "boost": 10
                                            }
                                        }
                                    },
                                    {
                                        "match": {
                                            "cname.normalized_keyword": {
                                                "query": normed,
                                                "boost": 10
                                            }
                                        }
                                    }
                                ]
                            }
                        },
                        {
                            "match": {
                                "whitelist": {
                                    "query": normed
                                }
                            }
                        },
                        {
                            "match": {
                                "cname": {
                                    "query": normed
                                }
                            }
                        },
                        {
                            "match": {
                                "cname.char_ngram": {
                                    "query": normed
                                }
                            }
                        },
                        {
                            "match": {
                                "whitelist.char_ngram": {
                                    "query": normed
                                }
                            }
                        }
                    ]
                }
            },
            "size": 0,
            "aggs": {
                "limit_results": {
                    "sampler": {
                        "shard_size": 20
                    },
                    "aggs": {
                        "top_cnames": {
                            "terms": {
                                "field": "cname.raw",
                                "size": 100
                            },
                            "aggs": {
                                "top_text_rel_match": {
                                    "top_hits": {
                                        "size": 1
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }

        response = self._es_client.search(index=self._es_index_name, body=full_text_query)
        buckets = response['aggregations']['limit_results']['top_cnames']['buckets']
        results = [{'cname': bucket['key'],
                    'max_score': bucket['top_text_rel_match']['hits']['max_score'],
                    'num_hits': bucket['top_text_rel_match']['hits']['total']}
                   for bucket in buckets]
        results.sort(key=lambda x: x['max_score'], reverse=True)
        return results[0:10]

    def predict_proba(self, entity):
        """Runs prediction on a given entity and generates multiple hypotheses with their
        associated probabilities using the trained entity resolution model

        Args:
            entity (Entity): An entity found in an input query

        Returns:
            list: a list of tuples of the form (str, float) grouping resolved values and their
                probabilities
        """
        pass

    def evaluate(self, use_blind=False):
        """Evaluates the trained entity resolution model on the given test data

        Returns:
            TYPE: Description
        """
        pass

    def dump(self, model_path):
        """Persists the trained entity resolution model to disk.

        Args:
            model_path (str): The location on disk where the model should be stored
        """
        # joblib.dump(self._model, model_path)
        pass

    def load(self):
        """Loads the trained entity resolution model from disk

        Args:
            model_path (str): The location on disk where the model is stored
        """
        self.fit()
