"""
Transform module
"""

import os
import pickle

import numpy as np

from .action import Action


class Transform:
    """
    Executes a transform. Processes a stream of documents, loads batches into enabled data stores and vectorizes documents.
    """

    def __init__(self, embeddings, action):
        """
        Creates a new transform.

        Args:
            embeddings: embeddings instance
            action: index action
        """

        self.embeddings = embeddings
        self.action = action

        # Alias embeddings attributes
        self.config = embeddings.config
        self.delete = embeddings.delete
        self.model = embeddings.model
        self.database = embeddings.database
        self.graph = embeddings.graph
        self.indexes = embeddings.indexes
        self.scoring = embeddings.scoring if embeddings.issparse() else None

        # Get config parameters
        self.offset = embeddings.config.get("offset", 0) if action == Action.UPSERT else 0
        self.batch = embeddings.config.get("batch", 1024)

        # Transform columns
        columns = embeddings.config.get("columns", {})
        self.text = columns.get("text", "text")
        self.object = columns.get("object", "object")

        # Check if top-level indexing is enabled for this embeddings
        self.indexing = embeddings.model or embeddings.scoring

        # List of deleted ids with this action
        self.deletes = set()

    def __call__(self, documents, buffer):
        """
        Processes an iterable collection of documents, handles any iterable including generators.

        This method loads a stream of documents into enabled data stores and vectorizes documents into an embeddings array.

        Args:
            documents: iterable of (id, data, tags)
            buffer: file path used for memmap buffer

        Returns:
            (document ids, dimensions, embeddings)
        """

        # Return parameters
        ids, dimensions, embeddings = None, None, None

        if self.model:
            ids, dimensions, embeddings = self.vectors(documents, buffer)
        else:
            ids = self.ids(documents)

        return (ids, dimensions, embeddings)

    def vectors(self, documents, buffer):
        """
        Runs a vectors transform operation when dense indexing is enabled.

        Args:
            documents: iterable of (id, data, tags)
            buffer: file path used for memmap buffer

        Returns:
            (document ids, dimensions, embeddings)
        """

        # Consume stream and transform documents to vectors
        ids, dimensions, batches, stream = self.model.index(self.stream(documents), self.batch)

        # Check that embeddings are available and load as a memmap
        embeddings = None
        if ids:
            embeddings = np.memmap(buffer, dtype=np.float32, shape=(len(ids), dimensions), mode="w+")
            with open(stream, "rb") as queue:
                x = 0
                for _ in range(batches):
                    batch = pickle.load(queue)
                    embeddings[x : x + batch.shape[0]] = batch
                    x += batch.shape[0]

        # Remove temporary file
        os.remove(stream)

        return (ids, dimensions, embeddings)

    def ids(self, documents):
        """
        Runs an ids transform operation when dense indexing is disabled.

        Args:
            documents: iterable of (id, data, tags)

        Returns:
            document ids
        """

        # Consume stream and build extract ids
        ids = []
        for uid, _, _ in self.stream(documents):
            ids.append(uid)

        # Save offset when dense indexing is disabled
        self.config["offset"] = self.offset

        return ids

    def stream(self, documents):
        """
        This method does two things:

        1. Filter and yield data to vectorize
        2. Batch and load original documents into enabled data stores (database, graph, scoring)

        Documents are yielded for vectorization if one of the following is True:
            - dict with a text or object field
            - not a dict

        Otherwise, documents are only batched and inserted into data stores

        Args:
            documents: iterable collection (id, data, tags)
        """

        # Batch and index offset. Index offset increments by count of documents streamed for vectorization
        batch, offset = [], 0

        # Iterate and process documents stream
        for document in documents:
            if isinstance(document[1], dict):
                # Set text field to uid when top-level indexing is disabled and text empty
                if not self.indexing and not document[1].get(self.text):
                    document[1][self.text] = str(document[0])

                if self.text in document[1]:
                    yield (document[0], document[1][self.text], document[2])
                    offset += 1
                elif self.object in document[1]:
                    yield (document[0], document[1][self.object], document[2])
                    offset += 1
            else:
                yield document
                offset += 1

            # Batch document
            batch.append(document)
            if len(batch) == self.batch:
                self.load(batch, offset)
                batch, offset = [], 0

        # Final batch
        if batch:
            self.load(batch, offset)

    def load(self, batch, offset):
        """
        Loads a document batch. This method deletes existing ids from an embeddings index and
        loads into enabled data stores (database, graph, scoring).

        Args:
            batch: list of (id, data, tags)
            offset: index offset for batch
        """

        # Delete from embeddings index first (which deletes from underlying indexes and datastores) if this is an upsert
        if self.action == Action.UPSERT:
            # Get list of ids not yet seen and deleted
            deletes = [uid for uid, _, _ in batch if uid not in self.deletes]
            if deletes:
                # Execute delete
                self.delete(deletes)

                # Save deleted ids as a delete must only occur once per action
                self.deletes.update(deletes)

        # Load batch into database except if this is a reindex
        if self.database and self.action != Action.REINDEX:
            self.database.insert(batch, self.offset)

        # Load batch into scoring
        if self.scoring:
            self.scoring.insert(batch, self.offset)

        # Load batch into subindex documents stream
        if self.indexes:
            self.indexes.insert(batch, self.offset)

        # Load batch into graph
        if self.graph:
            self.graph.insert(batch, self.offset)

        # Increment offset
        self.offset += offset
