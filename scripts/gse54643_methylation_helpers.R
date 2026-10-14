parse_gse54643_columns <- function(column_names) {
  pattern <- "^SN\\s*([0-9]+)\\s+V([0-9]+)\\s+(Unmethylated Signal|Methylated Signal|Detection Pval)$"
  matches <- regexec(pattern, column_names, ignore.case = TRUE)
  parsed <- regmatches(column_names, matches)
  if (any(lengths(parsed) != 4L)) {
    bad <- column_names[lengths(parsed) != 4L]
    stop("Unrecognized GSE54643 matrix column(s): ", paste(head(bad, 5L), collapse = ", "))
  }
  values <- do.call(rbind, lapply(parsed, function(x) x[-1L]))
  result <- data.frame(
    matrix_column = seq_along(column_names),
    subject_id = paste0("SN", values[, 1L]),
    visit_code = paste0("V", values[, 2L]),
    timepoint = ifelse(values[, 2L] == "2", "baseline", ifelse(values[, 2L] == "6", "8w", NA_character_)),
    measure = tolower(gsub("[^A-Za-z]", "", values[, 3L])),
    stringsAsFactors = FALSE
  )
  if (anyNA(result$timepoint)) stop("Expected only V2 baseline and V6 8-week visits.")
  result$sample_key <- paste(result$subject_id, result$visit_code, sep = "_")
  sample_measure <- paste(result$sample_key, result$measure, sep = "|")
  if (anyDuplicated(sample_measure)) stop("Duplicate measurement column for a sample.")
  required_measures <- c("unmethylatedsignal", "methylatedsignal", "detectionpval")
  sample_keys <- unique(result$sample_key)
  for (sample_key in sample_keys) {
    observed <- result$measure[result$sample_key == sample_key]
    if (!setequal(observed, required_measures) || length(observed) != length(required_measures)) {
      stop("Each sample must have one unmethylated, methylated, and detection p-value column: ", sample_key)
    }
  }
  sample_rows <- unique(result[c("sample_key", "subject_id", "visit_code", "timepoint")])
  if (anyDuplicated(sample_rows$sample_key)) stop("Sample labels are not unique.")
  visit_counts <- table(sample_rows$subject_id, sample_rows$timepoint)
  if (!all(c("baseline", "8w") %in% colnames(visit_counts)) || any(visit_counts[, c("baseline", "8w"), drop = FALSE] != 1L)) {
    stop("Every subject must have exactly one baseline and one 8-week sample.")
  }
  if (nrow(visit_counts) != 10L || nrow(sample_rows) != 20L) {
    stop("Expected 10 independent subjects with 20 paired samples.")
  }
  result
}

calculate_gse54643_pairs <- function(methylated, unmethylated, detection_p, sample_info,
                                     detection_threshold = 0.01, min_pairs = 8L) {
  if (!identical(dim(methylated), dim(unmethylated)) || !identical(dim(methylated), dim(detection_p))) {
    stop("Methylated, unmethylated, and detection matrices must have identical dimensions.")
  }
  if (ncol(methylated) != nrow(sample_info) || anyDuplicated(sample_info$sample_key)) {
    stop("Sample metadata do not match the signal matrix columns.")
  }
  if (any(!is.finite(methylated) | !is.finite(unmethylated) | methylated < 0 | unmethylated < 0, na.rm = TRUE)) {
    stop("Signal intensities must be finite and non-negative.")
  }
  beta <- methylated / (methylated + unmethylated + 100)
  m_value <- log2((methylated + 1) / (unmethylated + 1))
  names(sample_info$sample_key) <- NULL
  sample_info$detection_failure_fraction <- colMeans(detection_p > detection_threshold, na.rm = TRUE)
  sample_info$sample_qc_flag <- ifelse(sample_info$detection_failure_fraction > 0.05, "review", "pass")
  detected_fraction <- rowMeans(detection_p <= detection_threshold, na.rm = TRUE)
  subjects <- sort(unique(sample_info$subject_id))
  delta_beta <- delta_m <- matrix(NA_real_, nrow = nrow(beta), ncol = length(subjects),
                                 dimnames = list(rownames(beta), subjects))
  for (index in seq_along(subjects)) {
    subject <- subjects[[index]]
    base <- which(sample_info$subject_id == subject & sample_info$timepoint == "baseline")
    post <- which(sample_info$subject_id == subject & sample_info$timepoint == "8w")
    if (length(base) != 1L || length(post) != 1L) stop("Unpaired subject in sample metadata: ", subject)
    valid <- detection_p[, base] <= detection_threshold & detection_p[, post] <= detection_threshold &
      is.finite(beta[, base]) & is.finite(beta[, post]) & is.finite(m_value[, base]) & is.finite(m_value[, post])
    delta_beta[valid, index] <- beta[valid, post] - beta[valid, base]
    delta_m[valid, index] <- m_value[valid, post] - m_value[valid, base]
  }
  complete_pairs <- rowSums(is.finite(delta_m))
  keep <- detected_fraction >= 0.95 & complete_pairs >= min_pairs
  list(
    beta = beta,
    m_value = m_value,
    delta_beta = delta_beta[keep, , drop = FALSE],
    delta_m = delta_m[keep, , drop = FALSE],
    people_per_probe = complete_pairs[keep],
    probe_keep = keep,
    sample_info = sample_info
  )
}

summarize_paired_deltas <- function(delta) {
  n_people <- rowSums(is.finite(delta))
  mean_delta <- rowSums(delta, na.rm = TRUE) / n_people
  centered_sum_squares <- rowSums(delta ^ 2, na.rm = TRUE) - n_people * mean_delta ^ 2
  standard_error <- sqrt(pmax(0, centered_sum_squares / pmax(n_people - 1L, 1L))) / sqrt(n_people)
  list(mean = mean_delta, standard_error = standard_error, n_people = n_people)
}
