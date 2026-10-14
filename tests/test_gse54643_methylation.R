source("scripts/gse54643_methylation_helpers.R")

headers <- unlist(lapply(101:110, function(subject) {
  c(paste("SN", subject, "V2 Unmethylated Signal"),
    paste("SN", subject, "V2 Methylated Signal"),
    paste("SN", subject, "V2 Detection Pval"),
    paste("SN", subject, "V6 Unmethylated Signal"),
    paste("SN", subject, "V6 Methylated Signal"),
    paste("SN", subject, "V6 Detection Pval"))
}))
column_map <- parse_gse54643_columns(headers)
stopifnot(nrow(column_map) == 60L, length(unique(column_map$subject_id)) == 10L)
stopifnot(all(table(unique(column_map[c("subject_id", "timepoint")])$subject_id) == 2L))

set.seed(9)
probes <- paste0("cg", sprintf("%08d", 1:40))
sample_info <- unique(column_map[c("sample_key", "subject_id", "timepoint")])
sample_info <- sample_info[match(unique(column_map$sample_key), sample_info$sample_key), , drop = FALSE]
unmethylated <- matrix(900, nrow = length(probes), ncol = 20L,
                       dimnames = list(probes, sample_info$sample_key))
methylated <- matrix(600, nrow = length(probes), ncol = 20L,
                     dimnames = list(probes, sample_info$sample_key))
detection_p <- matrix(0, nrow = length(probes), ncol = 20L,
                      dimnames = list(probes, sample_info$sample_key))
post_columns <- which(sample_info$timepoint == "8w")
methylated[1:20, post_columns] <- methylated[1:20, post_columns] + 20
detection_p[21, 1:2] <- 0.02
paired <- calculate_gse54643_pairs(methylated, unmethylated, detection_p, sample_info)
stopifnot(nrow(paired$delta_m) == 39L, paired$people_per_probe[[1L]] == 10L)
stopifnot(mean(paired$delta_beta[1L, ], na.rm = TRUE) > 0)
beta_summary <- summarize_paired_deltas(paired$delta_beta)
stopifnot(beta_summary$n_people[[1L]] == 10L, is.finite(beta_summary$standard_error[[1L]]))
stopifnot(paired$sample_info$sample_qc_flag[[1L]] %in% c("pass", "review"))

bad <- headers[-1L]
stopifnot(inherits(try(parse_gse54643_columns(bad), silent = TRUE), "try-error"))
cat("GSE54643 helper checks passed\n")
