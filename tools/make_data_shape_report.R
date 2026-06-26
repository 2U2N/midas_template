#!/usr/bin/env Rscript

# Create a conservative, sanitized data-shape report in the vault.
#
# The report helps a human researcher describe data structure to an AI coding
# agent without exposing observations. It never prints file paths, raw rows,
# exact values, exact timestamps, extrema, free-text examples, or stack traces.

missing_strings <- c("", "na", "n/a", "nan", "null", "none", ".")
distinct_track_limit <- 10000

main <- function() {
  args <- parse_args(commandArgs(trailingOnly = TRUE))
  reports <- scan_input(
    input_path = args$input,
    output_path = args$output,
    allow_category_labels = args$allow_category_labels,
    category_min_count = max(args$category_min_count, 2)
  )
  writeLines(render_report(reports), con = args$output, useBytes = TRUE)
}

parse_args <- function(args) {
  parsed <- list(
    input = NULL,
    output = "data_shape_report.md",
    allow_category_labels = FALSE,
    category_min_count = 20
  )

  index <- 1
  while (index <= length(args)) {
    item <- args[[index]]
    if (item == "--input") {
      index <- index + 1
      parsed$input <- args[[index]]
    } else if (item == "--output") {
      index <- index + 1
      parsed$output <- args[[index]]
    } else if (item == "--allow-category-labels") {
      parsed$allow_category_labels <- TRUE
    } else if (item == "--category-min-count") {
      index <- index + 1
      parsed$category_min_count <- as.integer(args[[index]])
    } else {
      stop("Unknown argument. Use --input and --output.")
    }
    index <- index + 1
  }

  if (is.null(parsed$input)) {
    stop("Missing required --input argument.")
  }
  parsed
}

scan_input <- function(input_path, output_path, allow_category_labels, category_min_count) {
  if (file.exists(input_path) && !dir.exists(input_path)) {
    files <- input_path
  } else if (dir.exists(input_path)) {
    files <- list.files(input_path, recursive = TRUE, full.names = TRUE, all.files = FALSE, no.. = TRUE)
    files <- files[file.exists(files) & !dir.exists(files)]
    files <- files[normalizePath(files, mustWork = FALSE) != normalizePath(output_path, mustWork = FALSE)]
    files <- sort(files)
  } else {
    return(list(dataset_note("dataset_1", "unknown", "Input was not found.")))
  }

  reports <- list()
  for (i in seq_along(files)) {
    file_reports <- read_dataset(
      path = files[[i]],
      alias = paste0("dataset_", i),
      allow_category_labels = allow_category_labels,
      category_min_count = category_min_count
    )
    file_reports <- lapply(file_reports, function(report) {
      report$display_name <- basename(files[[i]])
      report
    })
    reports <- c(reports, file_reports)
  }
  if (length(reports) == 0) {
    return(list(dataset_note("dataset_1", "unknown", "No readable files were found.")))
  }
  reports
}

read_dataset <- function(path, alias, allow_category_labels, category_min_count) {
  ext <- tolower(tools::file_ext(path))
  safe_try <- function(expr, file_format) {
    tryCatch(expr, error = function(e) {
      list(dataset_note(alias, file_format, paste0("Reader failed safely (", class(e)[[1]], "); no details printed.")))
    })
  }

  if (ext == "csv") {
    return(safe_try(list(profile_data_frame(
      utils::read.csv(path, stringsAsFactors = FALSE, check.names = FALSE),
      alias, "CSV", allow_category_labels, category_min_count
    )), "CSV"))
  }
  if (ext %in% c("tsv", "tab")) {
    return(safe_try(list(profile_data_frame(
      utils::read.delim(path, stringsAsFactors = FALSE, check.names = FALSE),
      alias, "TSV", allow_category_labels, category_min_count
    )), "TSV"))
  }
  if (ext %in% c("json", "jsonl", "ndjson")) {
    return(read_json(path, alias, ext, allow_category_labels, category_min_count))
  }
  if (ext %in% c("rds", "rdata")) {
    return(read_r_data(path, alias, ext, allow_category_labels, category_min_count))
  }
  if (ext %in% c("xlsx", "xls")) {
    return(read_excel(path, alias, allow_category_labels, category_min_count))
  }
  if (ext %in% c("sav", "zsav", "por", "dta", "sas7bdat", "xpt")) {
    return(read_haven(path, alias, ext, allow_category_labels, category_min_count))
  }
  if (ext %in% c("parquet", "feather")) {
    return(read_arrow(path, alias, ext, allow_category_labels, category_min_count))
  }
  if (ext %in% c("sqlite", "sqlite3", "db")) {
    return(read_sqlite(path, alias, allow_category_labels, category_min_count))
  }
  if (ext == "duckdb") {
    return(read_duckdb(path, alias, allow_category_labels, category_min_count))
  }
  if (ext %in% c("zip", "tar", "gz", "tgz", "7z", "pdf", "docx", "txt", "png", "jpg", "jpeg", "mp3", "mp4")) {
    return(list(dataset_note(alias, extension_label(ext), "Unsupported file type. Not parsed in v1.")))
  }
  list(dataset_note(alias, extension_label(ext), "Unsupported or unknown file type. Not parsed."))
}

read_json <- function(path, alias, ext, allow_category_labels, category_min_count) {
  if (!requireNamespace("jsonlite", quietly = TRUE)) {
    return(list(dataset_note(alias, "JSON", "Optional dependency missing: jsonlite.")))
  }
  tryCatch({
    if (ext %in% c("jsonl", "ndjson")) {
      frame <- jsonlite::stream_in(file(path), verbose = FALSE)
    } else {
      obj <- jsonlite::fromJSON(path, flatten = TRUE)
      frame <- as.data.frame(obj, stringsAsFactors = FALSE)
    }
    list(profile_data_frame(frame, alias, toupper(ext), allow_category_labels, category_min_count))
  }, error = function(e) {
    list(dataset_note(alias, "JSON", paste0("Reader failed safely (", class(e)[[1]], "); no details printed.")))
  })
}

read_r_data <- function(path, alias, ext, allow_category_labels, category_min_count) {
  tryCatch({
    if (ext == "rds") {
      obj <- readRDS(path)
      return(list(profile_data_frame(as.data.frame(obj), alias, "RDS", allow_category_labels, category_min_count)))
    }
    env <- new.env(parent = emptyenv())
    object_names <- load(path, envir = env)
    reports <- list()
    object_index <- 1
    for (object_name in object_names) {
      obj <- get(object_name, envir = env)
      if (is.data.frame(obj)) {
        reports[[length(reports) + 1]] <- profile_data_frame(
          as.data.frame(obj),
          paste0(alias, "_object_", object_index),
          "RData object",
          allow_category_labels,
          category_min_count
        )
        object_index <- object_index + 1
      }
    }
    if (length(reports) == 0) {
      return(list(dataset_note(alias, "RData", "No tabular objects detected.")))
    }
    reports
  }, error = function(e) {
    list(dataset_note(alias, toupper(ext), paste0("Reader failed safely (", class(e)[[1]], "); no details printed.")))
  })
}

read_excel <- function(path, alias, allow_category_labels, category_min_count) {
  if (!requireNamespace("readxl", quietly = TRUE)) {
    return(list(dataset_note(alias, "Excel", "Optional dependency missing: readxl.")))
  }
  tryCatch({
    sheets <- readxl::excel_sheets(path)
    reports <- list()
    for (i in seq_along(sheets)) {
      frame <- readxl::read_excel(path, sheet = sheets[[i]])
      reports[[length(reports) + 1]] <- profile_data_frame(
        as.data.frame(frame),
        paste0(alias, "_sheet_", i),
        "Excel sheet",
        allow_category_labels,
        category_min_count
      )
    }
    reports
  }, error = function(e) {
    list(dataset_note(alias, "Excel", paste0("Reader failed safely (", class(e)[[1]], "); no details printed.")))
  })
}

read_haven <- function(path, alias, ext, allow_category_labels, category_min_count) {
  if (!requireNamespace("haven", quietly = TRUE)) {
    return(list(dataset_note(alias, extension_label(ext), "Optional dependency missing: haven.")))
  }
  tryCatch({
    if (ext %in% c("sav", "zsav")) {
      frame <- haven::read_sav(path)
      file_format <- "SPSS"
    } else if (ext == "por") {
      frame <- haven::read_por(path)
      file_format <- "SPSS portable"
    } else if (ext == "dta") {
      frame <- haven::read_dta(path)
      file_format <- "Stata"
    } else if (ext == "sas7bdat") {
      frame <- haven::read_sas(path)
      file_format <- "SAS"
    } else {
      frame <- haven::read_xpt(path)
      file_format <- "SAS transport"
    }
    list(profile_data_frame(as.data.frame(frame), alias, file_format, allow_category_labels, category_min_count))
  }, error = function(e) {
    list(dataset_note(alias, extension_label(ext), paste0("Reader failed safely (", class(e)[[1]], "); no details printed.")))
  })
}

read_arrow <- function(path, alias, ext, allow_category_labels, category_min_count) {
  if (!requireNamespace("arrow", quietly = TRUE)) {
    return(list(dataset_note(alias, extension_label(ext), "Optional dependency missing: arrow.")))
  }
  tryCatch({
    if (ext == "parquet") {
      frame <- arrow::read_parquet(path)
      file_format <- "Parquet"
    } else {
      frame <- arrow::read_feather(path)
      file_format <- "Feather"
    }
    list(profile_data_frame(as.data.frame(frame), alias, file_format, allow_category_labels, category_min_count))
  }, error = function(e) {
    list(dataset_note(alias, extension_label(ext), paste0("Reader failed safely (", class(e)[[1]], "); no details printed.")))
  })
}

read_sqlite <- function(path, alias, allow_category_labels, category_min_count) {
  if (!requireNamespace("DBI", quietly = TRUE) || !requireNamespace("RSQLite", quietly = TRUE)) {
    return(list(dataset_note(alias, "SQLite", "Optional dependency missing: DBI/RSQLite.")))
  }
  tryCatch({
    con <- DBI::dbConnect(RSQLite::SQLite(), dbname = path)
    on.exit(DBI::dbDisconnect(con), add = TRUE)
    tables <- sort(DBI::dbListTables(con))
    if (length(tables) == 0) {
      return(list(dataset_note(alias, "SQLite", "No tables detected.")))
    }
    reports <- list()
    for (i in seq_along(tables)) {
      frame <- DBI::dbReadTable(con, tables[[i]])
      reports[[length(reports) + 1]] <- profile_data_frame(
        as.data.frame(frame),
        paste0(alias, "_table_", i),
        "SQLite table",
        allow_category_labels,
        category_min_count
      )
    }
    reports
  }, error = function(e) {
    list(dataset_note(alias, "SQLite", paste0("Reader failed safely (", class(e)[[1]], "); no details printed.")))
  })
}

read_duckdb <- function(path, alias, allow_category_labels, category_min_count) {
  if (!requireNamespace("DBI", quietly = TRUE) || !requireNamespace("duckdb", quietly = TRUE)) {
    return(list(dataset_note(alias, "DuckDB", "Optional dependency missing: DBI/duckdb.")))
  }
  tryCatch({
    con <- DBI::dbConnect(duckdb::duckdb(), dbdir = path, read_only = TRUE)
    on.exit(DBI::dbDisconnect(con, shutdown = TRUE), add = TRUE)
    tables <- sort(DBI::dbListTables(con))
    if (length(tables) == 0) {
      return(list(dataset_note(alias, "DuckDB", "No tables detected.")))
    }
    reports <- list()
    for (i in seq_along(tables)) {
      frame <- DBI::dbReadTable(con, tables[[i]])
      reports[[length(reports) + 1]] <- profile_data_frame(
        as.data.frame(frame),
        paste0(alias, "_table_", i),
        "DuckDB table",
        allow_category_labels,
        category_min_count
      )
    }
    reports
  }, error = function(e) {
    list(dataset_note(alias, "DuckDB", paste0("Reader failed safely (", class(e)[[1]], "); no details printed.")))
  })
}

dataset_note <- function(alias, file_format, note) {
  list(kind = "note", alias = alias, file_format = file_format, note = note)
}

profile_data_frame <- function(frame, alias, file_format, allow_category_labels, category_min_count) {
  frame <- as.data.frame(frame, stringsAsFactors = FALSE)
  columns <- lapply(names(frame), function(column_name) {
    profile_column(frame[[column_name]], column_name, allow_category_labels, category_min_count)
  })
  list(
    kind = "profile",
    alias = alias,
    file_format = file_format,
    row_count = nrow(frame),
    columns = columns
  )
}

profile_column <- function(values, column_name, allow_category_labels, category_min_count) {
  text_values <- normalize_values(values)
  missing <- is.na(values) | tolower(trimws(text_values)) %in% missing_strings
  nonmissing_values <- values[!missing]
  nonmissing_text <- text_values[!missing]
  nonmissing_n <- length(nonmissing_text)
  type <- infer_column_type(nonmissing_values, nonmissing_text, column_name)
  distinct_values <- unique(nonmissing_text)
  distinct_overflow <- length(distinct_values) > distinct_track_limit
  if (distinct_overflow) {
    distinct_values <- distinct_values[seq_len(distinct_track_limit)]
  }
  max_text_len <- if (nonmissing_n == 0) 0 else max(nchar(nonmissing_text, type = "chars", allowNA = FALSE))
  has_whitespace_text <- any(grepl("\\s", nonmissing_text))
  identifier_name <- grepl("(^|_)(id|uuid|guid|name|user|username|email|url|uri|phone|address|token|key)($|_)", column_name, ignore.case = TRUE)
  identifier_like <- identifier_name || (nonmissing_n >= 10 && (distinct_overflow || length(distinct_values) / max(nonmissing_n, 1) >= 0.9))
  free_text_like <- grepl("(text|body|content|message|comment|transcript|caption)", column_name, ignore.case = TRUE) ||
    max_text_len > 120 ||
    (max_text_len > 40 && has_whitespace_text)
  categorical_like <- nonmissing_n > 0 &&
    !distinct_overflow &&
    !free_text_like &&
    length(distinct_values) <= 30 &&
    length(distinct_values) <= max(5, nonmissing_n * 0.2)
  datetime_like <- type == "datetime_like" || grepl("(date|time|timestamp|created|updated)", column_name, ignore.case = TRUE)

  flags <- c()
  if (identifier_like) flags <- c(flags, "identifier_like")
  if (categorical_like) flags <- c(flags, "categorical_like")
  if (type == "numeric_like") flags <- c(flags, "numeric_like")
  if (datetime_like) flags <- c(flags, "datetime_like")
  if (free_text_like) flags <- c(flags, "free_text_like")
  if (identifier_name && !distinct_overflow && nonmissing_n > length(distinct_values)) {
    flags <- c(flags, "possible_duplicate_key_values")
  }
  if (identifier_name && distinct_overflow) {
    flags <- c(flags, "duplicate_check_not_assessed")
  }

  list(
    name = column_name,
    inferred_type = choose_inferred_type(type, free_text_like, categorical_like),
    missingness = missingness_bucket(sum(missing), length(values)),
    text_length = text_length_bucket(max_text_len),
    flags = if (length(flags) == 0) "none" else paste(flags, collapse = ", "),
    category_labels = category_labels(nonmissing_text, categorical_like, allow_category_labels, category_min_count)
  )
}

choose_inferred_type <- function(type, free_text_like, categorical_like) {
  if (free_text_like) {
    return("free_text_like")
  }
  if (type %in% c("datetime_like", "numeric_like", "boolean_like", "all_missing")) {
    return(type)
  }
  if (categorical_like) {
    return("categorical_like")
  }
  type
}

infer_column_type <- function(values, text_values, column_name) {
  if (length(text_values) == 0) {
    return("all_missing")
  }
  if (inherits(values, "Date") || inherits(values, "POSIXt")) {
    return("datetime_like")
  }
  if (is.numeric(values) || is.integer(values)) {
    return("numeric_like")
  }
  if (is.logical(values)) {
    return("boolean_like")
  }
  numeric_like <- suppressWarnings(!is.na(as.numeric(text_values)))
  boolean_like <- tolower(text_values) %in% c("true", "false", "yes", "no")
  datetime_like <- grepl("^\\d{4}-\\d{2}-\\d{2}|^\\d{2}/\\d{2}/\\d{4}|^\\d{2}\\.\\d{2}\\.\\d{4}|^\\d{4}/\\d{2}/\\d{2}", text_values)
  if (mean(datetime_like) >= 0.8) return("datetime_like")
  if (mean(numeric_like) >= 0.8) return("numeric_like")
  if (mean(boolean_like) >= 0.8) return("boolean_like")
  "text_or_mixed"
}

category_labels <- function(values, categorical_like, allow_category_labels, category_min_count) {
  if (!allow_category_labels || !categorical_like) {
    return("suppressed")
  }
  values <- values[nchar(values) > 0 & nchar(values) <= 60 & !grepl("[\r\n]", values)]
  counts <- sort(table(values), decreasing = TRUE)
  labels <- names(counts[counts >= category_min_count])
  if (length(labels) == 0) {
    return("none printed")
  }
  paste(vapply(head(labels, 10), markdown_cell, character(1)), collapse = "; ")
}

normalize_values <- function(values) {
  values <- as.character(values)
  values[is.na(values)] <- ""
  trimws(values)
}

render_report <- function(reports) {
  lines <- c(
    "# Sanitized Data Shape Report",
    "",
    "Review this report before copying any part of it into `PROJECT_BRIEF.md`.",
    "",
    "The report suppresses file paths, raw rows, exact values, exact timestamps, extrema, free-text examples, stack traces, and exact row counts.",
    "",
    "Column names are printed because they are often needed for code generation. Remove or generalize any column name that is itself identifying before sharing with Midas.",
    "",
    "Base file names are printed to help users match reports to their data files. Remove or generalize file names before sharing if the names contain sensitive details.",
    ""
  )
  for (report in reports) {
    if (identical(report$kind, "note")) {
      lines <- c(lines, render_note(report))
    } else {
      lines <- c(lines, render_profile(report))
    }
  }
  paste(lines, collapse = "\n")
}

render_note <- function(note) {
  c(
    paste0("## ", markdown_cell(note$alias)),
    "",
    paste0("- file name: ", markdown_cell(if (is.null(note$display_name)) "not available" else note$display_name)),
    paste0("- format: ", markdown_cell(note$file_format)),
    paste0("- status: ", markdown_cell(note$note)),
    ""
  )
}

render_profile <- function(profile) {
  lines <- c(
    paste0("## ", markdown_cell(profile$alias)),
    "",
    paste0("- file name: ", markdown_cell(if (is.null(profile$display_name)) "not available" else profile$display_name)),
    paste0("- format: ", markdown_cell(profile$file_format)),
    paste0("- row count: ", row_bucket(profile$row_count)),
    paste0("- column count: ", length(profile$columns)),
    "",
    "| column | inferred type | missingness | text length | flags | category labels |",
    "|---|---|---|---|---|---|"
  )
  for (column in profile$columns) {
    lines <- c(lines, paste0(
      "| ",
      paste(
        c(
          markdown_cell(column$name),
          markdown_cell(column$inferred_type),
          markdown_cell(column$missingness),
          markdown_cell(column$text_length),
          markdown_cell(column$flags),
          markdown_cell(column$category_labels)
        ),
        collapse = " | "
      ),
      " |"
    ))
  }
  c(lines, "")
}

row_bucket <- function(count) {
  if (count == 0) return("0")
  if (count < 10) return("1-9")
  if (count < 100) return("10-99")
  if (count < 1000) return("100-999")
  if (count < 10000) return("1,000-9,999")
  if (count < 100000) return("10,000-99,999")
  if (count < 1000000) return("100,000-999,999")
  "1,000,000+"
}

missingness_bucket <- function(missing, total) {
  if (total == 0) return("not assessed")
  if (missing == 0) return("none observed")
  rate <- missing / total
  if (rate < 0.01) return("<1%")
  if (rate < 0.05) return("1-5%")
  if (rate < 0.20) return("5-20%")
  if (rate < 0.50) return("20-50%")
  if (rate < 1.0) return(">50%")
  "all missing"
}

text_length_bucket <- function(max_len) {
  if (max_len == 0) return("none")
  if (max_len <= 20) return("short")
  if (max_len <= 120) return("medium")
  if (max_len <= 500) return("long")
  "very long"
}

markdown_cell <- function(value) {
  value <- as.character(value)
  value <- gsub("\\\\", "\\\\\\\\", value)
  value <- gsub("\\|", "\\\\|", value)
  value <- gsub("[\r\n]", " ", value)
  value
}

extension_label <- function(ext) {
  if (is.na(ext) || ext == "") {
    return("unknown")
  }
  toupper(ext)
}

main()
