"""
Defines classes representing sets of SARIF files, individual SARIF files and runs within SARIF
files, along with associated functions and constants.
"""

import os
import re
from typing import Iterator

SARIF_SEVERITIES = ["error", "warning", "note"]

RECORD_ATTRIBUTES = ["Tool", "Severity", "Code", "Location", "Line"]

# Standard time format, e.g. `20211012T110000Z` (not part of the SARIF standard).
# Can obtain from bash via `date +"%Y%m%dT%H%M%SZ"``
DATETIME_REGEX = r"\d{8}T\d{6}Z"

_SLASHES = ["\\", "/"]


def has_sarif_file_extension(filename):
    """
    As per section 3.2 of the SARIF standard, SARIF filenames SHOULD end in ".sarif" and MAY end in
    ".sarif.json".
    https://docs.oasis-open.org/sarif/sarif/v2.1.0/os/sarif-v2.1.0-os.html#_Toc34317421
    """
    filename_upper = filename.upper().strip()
    return any(filename_upper.endswith(x) for x in [".SARIF", ".SARIF.JSON"])


def _group_records_by_severity(records) -> dict[str, list[dict]]:
    """
    Get the records, grouped by severity.
    """
    return {
        severity: [record for record in records if record["Severity"] == severity]
        for severity in SARIF_SEVERITIES
    }


def _count_records_by_issue_code(records, severity) -> list[tuple]:
    """
    Return a list of pairs (code, count) of the records with the specified
    severities.
    """
    code_to_count = {}
    for record in records:
        if record["Severity"] == severity:
            code = record["Code"]
            code_to_count[code] = code_to_count.get(code, 0) + 1
    return sorted(code_to_count.items(), key=lambda x: x[1], reverse=True)


class SarifRun:
    """
    Class to hold a run object from a SARIF file (an entry in the top-level "runs" list
    in a SARIF file), as defined in SARIF standard section 3.14.
    https://docs.oasis-open.org/sarif/sarif/v2.1.0/os/sarif-v2.1.0-os.html#_Toc34317484
    """

    def __init__(self, sarif_file_object, run_index, run_data):
        self.sarif_file = sarif_file_object
        self.run_index = run_index
        self.run_data = run_data
        self._path_prefixes_upper = None
        self._cached_records = None

    def init_path_prefix_stripping(self, autotrim=False, path_prefixes=None):
        """
        Set up path prefix stripping.  When records are subsequently obtained, the start of the
        path is stripped.
        If no path_prefixes are specified, the default behaviour is to strip the common prefix
        from each run.
        If path prefixes are specified, the specified prefixes are stripped.
        """
        prefixes = []
        if path_prefixes:
            prefixes = [prefix.strip().upper() for prefix in path_prefixes]
        if autotrim:
            autotrim_prefix = None
            records = self.get_records()
            if len(records) == 1:
                loc = records[0]["Location"].strip()
                slash_pos = max(loc.rfind(slash) for slash in _SLASHES)
                autotrim_prefix = loc[0:slash_pos] if slash_pos > -1 else None
            elif len(records) > 1:
                common_prefix = records[0]["Location"].strip()
                for record in records[1:]:
                    for (char_pos, char) in enumerate(record["Location"].strip()):
                        if char_pos >= len(common_prefix):
                            break
                        if char != common_prefix[char_pos]:
                            common_prefix = common_prefix[0:char_pos]
                            break
                    if not common_prefix:
                        break
                if common_prefix:
                    autotrim_prefix = common_prefix.upper()
            if autotrim_prefix and not any(
                p.startswith(autotrim_prefix.strip().upper()) for p in prefixes
            ):
                prefixes.append(autotrim_prefix)
        self._path_prefixes_upper = prefixes or None
        # Clear the untrimmed records cached by get_records() above.
        self._cached_records = None

    def get_tool_name(self) -> str:
        """
        Get the tool name from this run.
        """
        return self.run_data["tool"]["driver"]["name"]

    def get_results(self) -> list[dict]:
        """
        Get the results from this run.  These are the Result objects as defined in the
        SARIF standard section 3.27.
        https://docs.oasis-open.org/sarif/sarif/v2.1.0/os/sarif-v2.1.0-os.html#_Toc34317638
        """
        return self.run_data["results"]

    def get_records(self) -> list[dict]:
        """
        Get simplified records derived from the results of this run.  The records have the
        keys defined in `RECORD_ATTRIBUTES`.
        """
        if not self._cached_records:
            results = self.get_results()
            self._cached_records = [self.result_to_record(result) for result in results]
        return self._cached_records

    def get_records_grouped_by_severity(self) -> dict[str, list[dict]]:
        """
        Get the records, grouped by severity.
        """
        return _group_records_by_severity(self.get_records())

    def result_to_record(self, result):
        """
        Convert a SARIF result object to a simple record with fields "Tool", "Location", "Line",
        "Severity" and "Code".
        See definition of result object here:
        https://docs.oasis-open.org/sarif/sarif/v2.1.0/os/sarif-v2.1.0-os.html#_Toc34317638
        """
        error_id = result["ruleId"]
        locations = result.get("locations", [])
        error_line = "1"
        file_path = None
        tool_name = self.get_tool_name()
        if locations:
            location = locations[0]
            physical_location = location.get("physicalLocation", {})
            # SpotBugs has some errors with no line number so deal with them by just leaving it at 1
            error_line = physical_location.get("region", {}).get(
                "startLine", error_line
            )
            # For file name, first try the location written by DevSkim
            file_path = (
                location.get("physicalLocation", {})
                .get("address", {})
                .get("fullyQualifiedName", None)
            )
            if not file_path:
                # Next try the physical location written by MobSF and by SpotBugs (for some errors)
                file_path = (
                    location.get("physicalLocation", {})
                    .get("artifactLocation", {})
                    .get("uri", None)
                )
            if not file_path:
                logical_locations = location.get("logicalLocations", None)
                if logical_locations:
                    # Finally, try the logical location written by SpotBugs for some errors
                    file_path = logical_locations[0].get("fullyQualifiedName", None)
        if not file_path:
            raise ValueError(f"No location in {error_id} output from {tool_name}")

        if self._path_prefixes_upper:
            file_path_upper = file_path.upper()
            for prefix in self._path_prefixes_upper:
                if file_path_upper.startswith(prefix):
                    prefixlen = len(prefix)
                    if len(file_path) > prefixlen and file_path[prefixlen] in _SLASHES:
                        # Strip off trailing path separator
                        file_path = file_path[prefixlen + 1 :]
                    else:
                        file_path = file_path[prefixlen:]
                    break

        # Get the error severity, if included, and code
        severity = result.get(
            "level", "warning"
        )  # If an error has no specified level then by default it is a warning
        message = result["message"]["text"]

        # Create a dict representing this result
        record = {
            "Tool": tool_name,
            "Location": file_path,
            "Line": error_line,
            "Severity": severity,
            "Code": f"{error_id} {message}",
        }
        return record

    def get_result_count(self) -> int:
        """
        Return the total number of results.
        """
        return len(self.get_results())

    def get_result_count_by_severity(self) -> dict[str, int]:
        """
        Return a dict from SARIF severity to number of records.
        """
        records = self.get_records()
        return {
            severity: sum(1 for record in records if severity in record["Severity"])
            for severity in SARIF_SEVERITIES
        }

    def get_issue_code_histogram(self, severity) -> list[tuple]:
        """
        Return a list of pairs (code, count) of the records with the specified
        severities.
        """
        return _count_records_by_issue_code(self.get_records(), severity)


class SarifFile:
    """
    Class to hold SARIF data parsed from a file and provide accesssors to the data.
    """

    def __init__(self, file_path, data):
        self.abs_file_path = os.path.abspath(file_path)
        self.data = data
        self.runs = [
            SarifRun(self, run_index, run_data)
            for (run_index, run_data) in enumerate(self.data.get("runs", []))
        ]

    def __bool__(self):
        """
        True if non-empty.
        """
        return bool(self.runs)

    def init_path_prefix_stripping(self, autotrim=False, path_prefixes=None):
        """
        Set up path prefix stripping.  When records are subsequently obtained, the start of the
        path is stripped.
        If no path_prefixes are specified, the default behaviour is to strip the common prefix
        from each run.
        If path prefixes are specified, the specified prefixes are stripped.
        """
        for run in self.runs:
            run.init_path_prefix_stripping(autotrim, path_prefixes)

    def get_abs_file_path(self) -> str:
        """
        Get the absolute file path from which this SARIF data was loaded.
        """
        return self.abs_file_path

    def get_file_name(self) -> str:
        """
        Get the file name from which this SARIF data was loaded.
        """
        return os.path.basename(self.abs_file_path)

    def get_file_name_without_extension(self) -> str:
        """
        Get the file name from which this SARIF data was loaded, without extension.
        """
        file_name = self.get_file_name()
        return file_name[0 : file_name.index(".")] if "." in file_name else file_name

    def get_file_name_extension(self) -> str:
        """
        Get the extension of the file name from which this SARIF data was loaded.
        """
        file_name = self.get_file_name()
        return file_name[file_name.index(".") + 1 :] if "." in file_name else ""

    def get_filename_timestamp(self) -> str:
        """
        Extract the timestamp from the filename and return the date-time string extracted.
        """
        parsed_date = re.findall(DATETIME_REGEX, self.get_file_name())
        return parsed_date if len(parsed_date) == 1 else None

    def get_distinct_tool_names(self):
        """
        Return a list of tool names that feature in the runs in this file.
        The list is deduplicated and sorted into alphabetical order.
        """
        return sorted(list(set(run.get_tool_name() for run in self.runs)))

    def get_results(self) -> list[dict]:
        """
        Get the results from all runs in this file.  These are the Result objects as defined in the
        SARIF standard section 3.27.
        https://docs.oasis-open.org/sarif/sarif/v2.1.0/os/sarif-v2.1.0-os.html#_Toc34317638
        """
        ret = []
        for run in self.runs:
            ret += run.get_results()
        return ret

    def get_records(self) -> list[dict]:
        """
        Get simplified records derived from the results of all runs.  The records have the
        keys defined in `RECORD_ATTRIBUTES`.
        """
        ret = []
        for run in self.runs:
            ret += run.get_records()
        return ret

    def get_records_grouped_by_severity(self) -> dict[str, list[dict]]:
        """
        Get the records, grouped by severity.
        """
        return _group_records_by_severity(self.get_records())

    def get_result_count(self) -> int:
        """
        Return the total number of results.
        """
        return sum(run.get_result_count() for run in self.runs)

    def get_result_count_by_severity(self) -> dict[str, int]:
        """
        Return a dict from SARIF severity to number of records.
        """
        get_result_count_by_severity_per_run = [
            run.get_result_count_by_severity() for run in self.runs
        ]
        return {
            severity: sum(
                rc.get(severity, 0) for rc in get_result_count_by_severity_per_run
            )
            for severity in SARIF_SEVERITIES
        }

    def get_issue_code_histogram(self, severity) -> list[tuple]:
        """
        Return a list of pairs (code, count) of the records with the specified
        severities.
        """
        return _count_records_by_issue_code(self.get_records(), severity)


class SarifFileSet:
    """
    Class representing a set of SARIF files.
    The "composite" pattern is used to allow multiple subdirectories.
    """

    def __init__(self):
        self.subdirs = []
        self.files = []

    def __bool__(self):
        """
        Return true if there are any SARIF files, regardless of whether they contain any runs.
        """
        return any(bool(subdir) for subdir in self.subdirs) or bool(self.files)

    def __len__(self):
        """
        Return the number of SARIF files, in total.
        """
        return sum(len(subdir) for subdir in self.subdirs) + sum(
            1 for f in self.files if f
        )

    def __iter__(self) -> Iterator[SarifFile]:
        """
        Iterate the SARIF files in this set.
        """
        for subdir in self.subdirs:
            for input_file in subdir.files:
                yield input_file
        for input_file in self.files:
            yield input_file

    def __getitem__(self, index) -> SarifFile:
        i = 0
        for subdir in self.subdirs:
            for input_file in subdir.files:
                if i == index:
                    return input_file
                i += 1
        return self.files[index - i]

    def get_description(self):
        """
        Get a description of the SARIF file set - the name of the single file or the number of
        files.
        """
        count = len(self)
        if count == 1:
            return self[0].get_file_name()
        return f"{count} files"

    def init_path_prefix_stripping(self, autotrim=False, path_prefixes=None):
        """
        Set up path prefix stripping.  When records are subsequently obtained, the start of the
        path is stripped.
        If no path_prefixes are specified, the default behaviour is to strip the common prefix
        from each run.
        If path prefixes are specified, the specified prefixes are stripped.
        """
        for subdir in self.subdirs:
            subdir.init_path_prefix_stripping(autotrim, path_prefixes)
        for input_file in self.files:
            input_file.init_path_prefix_stripping(autotrim, path_prefixes)

    def add_dir(self, sarif_file_set):
        """
        Add a SarifFileSet as a subdirectory.
        """
        self.subdirs.append(sarif_file_set)

    def add_file(self, sarif_file_object: SarifFile):
        """
        Add a single SARIF file to the set.
        """
        self.files.append(sarif_file_object)

    def get_distinct_tool_names(self) -> list[str]:
        """
        Return a list of tool names that feature in the runs in these files.
        The list is deduplicated and sorted into alphabetical order.
        """
        all_tool_names = set()
        for subdir in self.subdirs:
            all_tool_names.update(subdir.get_distinct_tool_names())
        for input_file in self.files:
            all_tool_names.update(input_file.get_distinct_tool_names())

        return sorted(list(all_tool_names))

    def get_results(self) -> list[dict]:
        """
        Get the results from all runs in all files.  These are the Result objects as defined in the
        SARIF standard section 3.27.
        https://docs.oasis-open.org/sarif/sarif/v2.1.0/os/sarif-v2.1.0-os.html#_Toc34317638
        """
        ret = []
        for subdir in self.subdirs:
            ret += subdir.get_results()
        for input_file in self.files:
            ret += input_file.get_results()
        return ret

    def get_records(self) -> list[dict]:
        """
        Get simplified records derived from the results of all runs.  The records have the
        keys defined in `RECORD_ATTRIBUTES`.
        """
        ret = []
        for subdir in self.subdirs:
            ret += subdir.get_records()
        for input_file in self.files:
            ret += input_file.get_records()
        return ret

    def get_records_grouped_by_severity(self) -> dict[str, list[dict]]:
        """
        Get the records, grouped by severity.
        """
        return _group_records_by_severity(self.get_records())

    def get_result_count(self) -> int:
        """
        Return the total number of results.
        """
        return sum(subdir.get_result_count() for subdir in self.subdirs) + sum(
            input_file.get_result_count() for input_file in self.files
        )

    def get_result_count_by_severity(self) -> dict[str, int]:
        """
        Return a dict from SARIF severity to number of records.
        """
        result_counts_by_severity = []
        for subdir in self.subdirs:
            result_counts_by_severity.append(subdir.get_result_count_by_severity())
        for input_file in self.files:
            result_counts_by_severity.append(input_file.get_result_count_by_severity())
        return {
            severity: sum(rc.get(severity, 0) for rc in result_counts_by_severity)
            for severity in SARIF_SEVERITIES
        }

    def get_issue_code_histogram(self, severity) -> list[tuple]:
        """
        Return a list of pairs (code, count) of the records with the specified
        severities.
        """
        return _count_records_by_issue_code(self.get_records(), severity)