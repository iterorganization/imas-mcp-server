import abc
from contextlib import contextmanager
from dataclasses import dataclass, field
import functools
import hashlib
import logging
from pathlib import Path
import time
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Literal,
    Optional,
    Set,
)
import xml.etree.ElementTree as ET

import imas_data_dictionary
from packaging.version import Version
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

IndexPrefixT = Literal["lexicographic", "semantic"]

# Performance tuning constants
DEFAULT_BATCH_SIZE = 500
PROGRESS_LOG_INTERVAL = 50

# Configure module logger
logger = logging.getLogger(__name__)


@dataclass
class DataDictionaryIndex(abc.ABC):
    """Abstract base class for IMAS Data Dictionary methods and attributes."""

    ids_set: Optional[Set[str]] = None  # Set of IDS names to index
    dirname: Path = field(
        default_factory=lambda: Path(__file__).resolve().parents[1] / "index"
    )
    indexname: Optional[str] = field(default=None)

    def __post_init__(self) -> None:
        """Common initialization for all handlers."""
        logger.info(f"Initializing DataDictionaryIndex with ids_set: {self.ids_set}")
        self.indexname = self._get_index_name()
        self.dirname.mkdir(parents=True, exist_ok=True)
        logger.info(
            f"Initialized Data Dictionary index: {self.indexname} in {self.dirname}"
        )

    @property
    @abc.abstractmethod
    def index_prefix(self) -> IndexPrefixT:
        """Return the index name prefix."""
        pass

    @contextmanager
    def _performance_timer(self, operation_name: str):
        """Context manager for timing operations with logging."""
        start_time = time.time()
        logger.info(f"Starting {operation_name}")
        try:
            yield
        finally:
            elapsed = time.time() - start_time
            logger.info(f"Completed {operation_name} in {elapsed:.2f}s")

    @functools.cached_property
    def _xml_root(self) -> ET.Element:
        """Cache XML root element for repeated access."""
        root = self._dd_etree.getroot()
        if root is None:
            raise ValueError("Root element not found in XML tree after parsing.")
        return root

    @functools.cached_property
    def _ids_elements(self) -> List[ET.Element]:
        """Cache IDS elements to avoid repeated XPath queries."""
        return self._xml_root.findall(".//IDS[@name]")

    @functools.cached_property
    def dd_version(self) -> Version:
        """Return the IMAS DD version."""
        return self._get_dd_version()

    def _get_dd_version(self) -> Version:
        """Return version from the IMAS DD XML tree."""
        root = self._dd_etree.getroot()
        assert root is not None, "Root element not found in XML tree after parsing."
        version_elem = root.find(".//version")
        if version_elem is None or version_elem.text is None:
            # More specific error if version tag or its text is missing
            raise ValueError(
                "Version element or its text content not found in XML tree"
            )
        return Version(version_elem.text)

    @functools.cached_property
    def _dd_etree(self) -> ET.ElementTree:
        """Return the IMAS DD XML element tree."""
        xml_path = imas_data_dictionary.get_xml_resource("IDSDef.xml")
        with xml_path.open("rb") as f:
            return ET.parse(f)  # type: ignore

    def _get_index_name(self) -> str:
        """Return the full index name based on prefix, IMAS DD version, and ids_set."""
        # Ensure dd_version is available
        dd_version = self.dd_version.public  # Access dd_version property
        indexname = f"{self.index_prefix}_{dd_version}"
        # Ensure ids_set is treated consistently (e.g., handle None or empty set)
        if self.ids_set is not None and len(self.ids_set) > 0:
            ids_str = ",".join(
                sorted(list(self.ids_set))
            )  # Convert set to sorted list for consistent hash
            hash_suffix = hashlib.md5(ids_str.encode("utf-8")).hexdigest()[
                :8
            ]  # Specify encoding
            return f"{indexname}-{hash_suffix}"
        return indexname

    def _get_ids_set(self) -> Set[str]:
        """Return a set of IDS names to process.
        If self.ids_set is provided, it's used. Otherwise, all IDS names from the DD are used.
        """
        if self.ids_set is not None:
            return self.ids_set

        logger.info(
            "No specific ids_set provided, using all IDS names from Data Dictionary."
        )
        all_ids_names: Set[str] = set()  # Explicit type
        # Ensure dd_etree is accessed correctly
        for elem in self._dd_etree.findall(
            ".//IDS[@name]"
        ):  # all IDS elements with a 'name' attribute
            name = elem.get("name")
            if name:  # Ensure name is not None or empty
                all_ids_names.add(name)
        if not all_ids_names:
            logger.warning("No IDS names found in the Data Dictionary XML.")
        return all_ids_names

    def _build_hierarchical_documentation(
        self, documentation_parts: Dict[str, str]
    ) -> str:
        """Build hierarchical documentation string from path-based documentation parts.

        Takes a dictionary of path-based documentation and formats it into a single
        hierarchical string with leaf node documentation prioritized first, followed
        by hierarchical context. This approach ensures the most specific and pertinent
        information appears immediately.

        Args:
            documentation_parts: Dictionary where keys are hierarchical paths
                (e.g., 'ids_name', 'ids_name/node', 'ids_name/node/subnode') and
                values are the documentation strings for each specific node.

        Returns:
            A formatted string with the leaf node documentation first, followed by
            hierarchical context with markdown headers. Empty string if no
            documentation parts are provided.

        Example:
            Input: {
                'core_profiles': 'Core plasma profiles data',
                'core_profiles/time': 'Time coordinate array',
                'core_profiles/profiles_1d': '1D profile data',
                'core_profiles/profiles_1d/temperature': 'Temperature profile'
            }

            Output:
            **core_profiles/profiles_1d/temperature**
            Temperature profile

            ## Hierarchical Context

            ### core_profiles
            Core plasma profiles data

            #### core_profiles/profiles_1d
            1D profile data

            ##### core_profiles/time
            Time coordinate array

        Note:
            - Leaf node (deepest path) documentation appears first for immediate relevance
            - Hierarchical context follows with proper header levels
            - Header levels are limited to 6 (markdown maximum), deeper levels use indentation
            - Empty documentation values are skipped
            - Paths in context section are sorted by depth then alphabetically
        """
        if not documentation_parts:
            return ""

        # Find the deepest (leaf) path efficiently
        paths_by_depth = sorted(documentation_parts.keys(), key=lambda x: x.count("/"))

        if not paths_by_depth:
            return ""

        deepest_path = paths_by_depth[-1]
        leaf_doc = documentation_parts.get(deepest_path, "")

        # Pre-allocate list for better memory usage
        doc_sections = []

        # Lead with leaf documentation if it exists
        if leaf_doc:
            doc_sections.append(f"**{deepest_path}**\n{leaf_doc}")

        # Add hierarchical context (excluding the leaf we already showed)
        remaining_paths = paths_by_depth[:-1]

        if remaining_paths:
            doc_sections.append("## Hierarchical Context")

            for path_key in remaining_paths:
                doc = documentation_parts[path_key]
                if doc:  # Only add if documentation exists
                    depth = path_key.count("/") + 1

                    if depth + 2 <= 6:
                        # Use markdown headers for levels 1-6
                        header_level = depth + 2
                        header = "#" * header_level
                        doc_sections.append(f"{header} {path_key}\n{doc}")
                    else:
                        # Use indentation for deeper levels (beyond 6)
                        indent_level = depth + 2 - 6  # How many levels beyond 6
                        indent = "  " * indent_level  # 2 spaces per level
                        doc_sections.append(
                            f"###### {indent}**{path_key}**\n{indent}{doc}"
                        )

        return "\n\n".join(doc_sections)

    def _build_element_entry(
        self,
        elem: ET.Element,
        ids_node: ET.Element,
        ids_name: str,
        parent_map: Dict[ET.Element, ET.Element],
    ) -> Optional[Dict[str, Any]]:
        """Build a single element entry efficiently."""
        path_parts = []
        documentation_parts = {}
        units = elem.get("units", "")

        # Walk up tree once
        walker = elem
        while walker is not None and walker != ids_node:
            walker_name = walker.get("name")
            if walker_name:
                path_parts.insert(0, walker_name)
                walker_doc = walker.get("documentation")
                if walker_doc:
                    doc_key = "/".join([ids_name] + path_parts)
                    documentation_parts[doc_key] = walker_doc

            # Handle units inheritance
            parent_walker = parent_map.get(walker)
            if units == "as_parent" and parent_walker is not None:
                parent_units = parent_walker.get("units")
                if parent_units:
                    units = parent_units

            walker = parent_walker

        if not path_parts:
            return None

        # Add IDS documentation
        ids_doc = ids_node.get("documentation")
        if ids_doc:
            documentation_parts[ids_name] = ids_doc

        full_path = f"{ids_name}/{'/'.join(path_parts)}"
        combined_documentation = self._build_hierarchical_documentation(
            documentation_parts
        )

        return {
            "path": full_path,
            "documentation": combined_documentation or elem.get("documentation", ""),
            "units": units or "none",
            "ids_name": ids_name,
        }

    @functools.cached_property
    def ids_names(self) -> List[str]:
        """Return a list of IDS names relevant to this index.
        Extracts from DD based on current configuration.
        """
        logger.info("Extracting IDS names from DD for current configuration.")

        # Use _get_ids_set() which respects self.ids_set if provided, or gets all from DD
        ids_set = self._get_ids_set()
        relevant_names = sorted(list(ids_set))  # Sort for consistency

        return relevant_names

    @contextmanager
    def _progress_tracker(self, description: str, total: Optional[int] = None):
        """Context manager for Rich progress tracking with standardized columns."""
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=20),
            MofNCompleteColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
        ) as progress:
            task = progress.add_task(description, total=total)
            yield progress, task

    @functools.cached_property
    def _total_elements(self) -> int:
        """
        Calculate and cache the total number of elements to process.        Returns:
            int: Total count of IDS root elements plus all their named descendants
        """
        ids_to_process = self._get_ids_set()
        root_node = self._xml_root

        # Pre-filter IDS nodes efficiently
        ids_nodes = [
            node
            for node in root_node.findall(".//IDS[@name]")
            if node.get("name") in ids_to_process
        ]

        total_elements = 0
        for ids_node in ids_nodes:
            total_elements += 1  # Count IDS root
            total_elements += len(ids_node.findall(".//*[@name]"))  # Count descendants

        logger.info(f"Total elements to process: {total_elements}")
        return total_elements

    def _get_document(self, progress_tracker=None) -> Iterable[Dict[str, Any]]:
        """
        Get document entries from IMAS Data Dictionary XML.

        Args:
            progress_tracker: Optional tuple of (progress, task) for external tracking        Yields:
            Dict[str, Any]: Document entries
        """
        logger.info(
            f"Starting optimized extraction for DD version {self.dd_version.public}"
        )

        ids_to_process = self._get_ids_set()
        logger.info(f"Processing {len(ids_to_process)} IDS: {sorted(ids_to_process)}")

        root_node = self._xml_root

        # Cache parent map - major performance improvement
        parent_map = {
            c: p for p in root_node.iter() for c in p
        }  # Pre-filter IDS nodes efficiently
        ids_nodes = [
            node
            for node in root_node.findall(".//IDS[@name]")
            if node.get("name") in ids_to_process
        ]

        if not ids_nodes:
            logger.warning(f"No IDS found for ids_set: {ids_to_process}")
            return  # Count total elements for progress tracking
        total_elements = (
            self._total_elements
        )  # Use provided progress tracker or create new one

        if progress_tracker:
            progress, task = progress_tracker
            # Don't update total when using shared progress tracker
            context_manager = None
        else:
            context_manager = self._progress_tracker(
                "Extracting Data Dictionary documents", total=total_elements
            )
            progress, task = context_manager.__enter__()

        try:
            document_count = 0

            for ids_node in ids_nodes:
                ids_name = ids_node.get("name")
                if not ids_name:
                    continue  # Yield IDS root entry
                yield {
                    "path": ids_name,
                    "documentation": ids_node.get("documentation", ""),
                    "units": ids_node.get("units", "none"),
                    "ids_name": ids_name,
                }
                document_count += 1
                if progress:
                    progress.advance(task)

                # Process all named descendants
                for elem in ids_node.findall(".//*[@name]"):
                    entry = self._build_element_entry(
                        elem, ids_node, ids_name, parent_map
                    )
                    if entry:
                        yield entry
                        document_count += 1
                        if progress:
                            progress.advance(task)

                            # Update description periodically for better time estimates
                            if document_count % PROGRESS_LOG_INTERVAL == 0:
                                progress.update(
                                    task,
                                    description=f"Processing {ids_name}",
                                )

        finally:
            # Only exit context manager if we created it
            if context_manager:
                context_manager.__exit__(None, None, None)

        logger.info(f"Finished extracting {document_count} document entries from DD.")

    def _get_document_batch(
        self, batch_size: int = DEFAULT_BATCH_SIZE
    ) -> Iterable[List[Dict[str, Any]]]:
        """
        Get document entries from Data Dictionary XML in batches.

        Args:
            batch_size: Number of documents per batch

        Yields:
            List[Dict[str, Any]]: Batches of document entries
        """
        logger.info(f"Generating document batches from index: {self.indexname}")

        # Use cached total elements calculation for accurate progress tracking
        total_elements = self._total_elements

        documents_batch = []
        processed_paths = set()
        total_documents = 0
        batch_count = 0

        # Single progress tracker shared with _get_document, with pre-calculated total
        with self._progress_tracker(
            "Processing IDS attributes", total=total_elements
        ) as (
            progress,
            task,
        ):
            try:  # Use shared progress tracker for consistent updates
                for entry_dict in self._get_document(progress_tracker=(progress, task)):
                    path = entry_dict.get("path")
                    if not path or path in processed_paths:
                        continue

                    if all(
                        key in entry_dict
                        for key in ["path", "documentation", "ids_name"]
                    ):
                        documents_batch.append(entry_dict)
                        processed_paths.add(path)
                        total_documents += 1

                        if len(documents_batch) >= batch_size:
                            batch_count += 1
                            yield list(documents_batch)
                            documents_batch.clear()

                if documents_batch:
                    batch_count += 1
                    yield list(documents_batch)

            except Exception as e:
                logger.error(f"Error during document batch generation: {e}")
                raise

        logger.info(
            f"Completed document batch generation: {batch_count} batches, {total_documents} total documents"
        )

    @abc.abstractmethod
    def build_index(self) -> None:
        """Builds the index from the Data Dictionary IDSDef XML file."""
        raise NotImplementedError
