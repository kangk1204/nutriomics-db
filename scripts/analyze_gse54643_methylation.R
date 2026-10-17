script_argument <- grep("^--file=", commandArgs(trailingOnly = FALSE), value = TRUE)
if (!length(script_argument)) stop("Run as an Rscript file.")
script_path <- normalizePath(sub("^--file=", "", script_argument[[1L]]), mustWork = TRUE)
source_root <- normalizePath(file.path(dirname(script_path), ".."))
project_root <- normalizePath(file.path(source_root, ".."))
r_library <- Sys.getenv("NUTRIGENOMICS_R_LIB", file.path(project_root, "toolchain", "r-library"))
if (dir.exists(r_library)) .libPaths(c(normalizePath(r_library), .libPaths()))
suppressPackageStartupMessages({
  library(limma)
  library(jsonlite)
  library(minfi)
  library(IlluminaHumanMethylation450kanno.ilmn12.hg19)
})
source(file.path(dirname(script_path), "gse54643_methylation_helpers.R"))

args <- commandArgs(trailingOnly = TRUE)
arg_value <- function(name, default) {
  index <- match(name, args)
  if (is.na(index) || index == length(args)) default else args[[index + 1L]]
}
data_root <- normalizePath(arg_value("--data-dir", Sys.getenv("NUTRIGENOMICS_DATA_DIR", "../data")), mustWork = TRUE)
output_root <- arg_value("--output-dir", file.path(dirname(data_root), "outputs", "gse54643-methylation-v1"))
matrix_path <- normalizePath(arg_value("--matrix", file.path(data_root, "raw", "geo", "GSE54643", "GSE54643_Matrix_signal_intensities.txt.gz")), mustWork = TRUE)
soft_path <- normalizePath(file.path(data_root, "raw", "geo", "GSE54643", "GSE54643_family.soft.gz"), mustWork = TRUE)
extraction_manifest_path <- normalizePath(file.path(data_root, "processed", "geo", "GSE54643", "raw_files", "extraction_manifest.json"), mustWork = TRUE)
download_manifest_path <- normalizePath(file.path(data_root, "raw", "geo", "download_manifest.json"), mustWork = TRUE)
lock_path <- normalizePath(file.path(project_root, "toolchain", "r-environment-linux-64.lock.txt"), mustWork = TRUE)
overlay_lock_path <- normalizePath(file.path(project_root, "toolchain", "r-overlay-lock.txt"), mustWork = TRUE)
dir.create(output_root, recursive = TRUE, showWarnings = FALSE)

header_connection <- gzfile(matrix_path, open = "rt")
header <- strsplit(readLines(header_connection, n = 1L, warn = FALSE), "\\t")[[1L]]
close(header_connection)
if (length(header) < 4L || header[[1L]] != "ID_REF") stop("Unexpected GSE54643 processed-matrix header.")
column_map <- parse_gse54643_columns(header[-1L])
classes <- c("character", rep("numeric", length(header) - 1L))
matrix_connection <- gzfile(matrix_path, open = "rt")
matrix_table <- read.delim(matrix_connection, check.names = FALSE,
                           stringsAsFactors = FALSE, colClasses = classes)
close(matrix_connection)
if (!identical(names(matrix_table), header)) stop("Matrix header changed while reading the file.")
probe_ids <- as.character(matrix_table[[1L]])
if (anyNA(probe_ids) || any(!nzchar(probe_ids)) || anyDuplicated(probe_ids)) stop("Probe IDs must be present and unique.")
signal <- as.matrix(matrix_table[-1L])
storage.mode(signal) <- "numeric"
rownames(signal) <- probe_ids
rm(matrix_table)
sample_info <- unique(column_map[c("sample_key", "subject_id", "visit_code", "timepoint")])
sample_info <- sample_info[match(unique(column_map$sample_key), sample_info$sample_key), , drop = FALSE]
sample_info$sample_label <- paste(gsub("^SN", "SN ", sample_info$subject_id), sample_info$visit_code)
sample_info$geo_sample_accession <- NA_character_
measure_column <- function(key, measure) {
  selected <- column_map$matrix_column[column_map$sample_key == key & column_map$measure == measure]
  if (length(selected) != 1L) stop("Missing or duplicate matrix measure for ", key, " / ", measure)
  selected
}
methylated <- unmethylated <- detection_p <- matrix(NA_real_, nrow = nrow(signal), ncol = nrow(sample_info),
                                                     dimnames = list(rownames(signal), sample_info$sample_key))
for (index in seq_len(nrow(sample_info))) {
  key <- sample_info$sample_key[[index]]
  methylated[, index] <- signal[, measure_column(key, "methylatedsignal")]
  unmethylated[, index] <- signal[, measure_column(key, "unmethylatedsignal")]
  detection_p[, index] <- signal[, measure_column(key, "detectionpval")]
}
rm(signal)
paired <- calculate_gse54643_pairs(methylated, unmethylated, detection_p, sample_info,
                                   detection_threshold = 0.01, min_pairs = 8L)
if (sum(paired$probe_keep) < 100000L) stop("Too few 450K probes pass the 95% detection and 8-pair criteria.")
sample_info <- paired$sample_info

probe_annotation <- as.data.frame(getAnnotation(IlluminaHumanMethylation450kanno.ilmn12.hg19), stringsAsFactors = FALSE)
annotation_index <- if ("Name" %in% names(probe_annotation)) match(rownames(paired$delta_m), as.character(probe_annotation$Name)) else match(rownames(paired$delta_m), rownames(probe_annotation))
if (sum(!is.na(annotation_index)) < 0.95 * nrow(paired$delta_m)) stop("CpG annotation crosswalk is incomplete.")
probe_annotation <- probe_annotation[annotation_index, , drop = FALSE]
gene_column <- intersect(c("UCSC_RefGene_Name", "UCSC_RefGene_Name.1"), names(probe_annotation))
gene_label <- if (length(gene_column)) as.character(probe_annotation[[gene_column[[1L]]]]) else rep(NA_character_, nrow(paired$delta_m))
chr_column <- intersect(c("chr", "CHR", "chromosome"), names(probe_annotation))
pos_column <- intersect(c("pos", "MAPINFO", "position"), names(probe_annotation))
chromosome <- if (length(chr_column)) as.character(probe_annotation[[chr_column[[1L]]]]) else rep(NA_character_, nrow(paired$delta_m))
position <- if (length(pos_column)) as.character(probe_annotation[[pos_column[[1L]]]]) else rep(NA_character_, nrow(paired$delta_m))

design <- matrix(1, nrow = ncol(paired$delta_m), ncol = 1L, dimnames = list(colnames(paired$delta_m), "mean_paired_change"))
fit <- eBayes(lmFit(paired$delta_m, design), robust = TRUE)
beta_summary <- summarize_paired_deltas(paired$delta_beta)
n_people <- beta_summary$n_people
standard_error <- fit$stdev.unscaled[, 1L] * sqrt(fit$s2.post)
results <- data.frame(
  feature_type = "cpg", feature_id = rownames(paired$delta_m),
  feature_label = ifelse(is.na(gene_label) | !nzchar(gene_label), rownames(paired$delta_m), gene_label),
  contrast = "mof_8w_vs_baseline", tissue = "whole blood", timepoint = "8w vs baseline",
  intervention = "Daily 200 mg monomeric and oligomeric flavanols from grape seeds; uncontrolled pre-post",
  dose = "200 mg/day", duration = "8 weeks", effect_estimate = beta_summary$mean,
  standard_error = beta_summary$standard_error, p_value = fit$p.value[, 1L], q_value = p.adjust(fit$p.value[, 1L], method = "BH"),
  effect_unit = "delta beta", status = "measured", n_people = n_people, n_samples = 2L * n_people,
  m_value_estimate = fit$coefficients[, 1L], m_value_standard_error = standard_error,
  genome_build = "hg19", annotation_gene = gene_label, chromosome = chromosome, position = position,
  cell_composition_adjusted = FALSE,
  metadata_json = vapply(seq_along(n_people), function(i) toJSON(list(inference_scope = "cohort-level exploratory paired association; not causal or individual prediction", paired_samples = n_people[[i]], gsm_crosswalk = "not present in the processed signal matrix"), auto_unbox = TRUE), character(1)),
  stringsAsFactors = FALSE
)
write_gzip_tsv <- function(data, path) {
  connection <- gzfile(path, open = "wt")
  on.exit(close(connection))
  write.table(data, connection, sep = "\t", quote = FALSE, row.names = FALSE, na = "NA")
}
write_gzip_tsv(results, file.path(output_root, "methylation_effects.tsv.gz"))
write.table(sample_info, file.path(output_root, "sample_metadata.tsv"), sep = "\t", quote = FALSE, row.names = FALSE, na = "NA")
write.table(data.frame(sample_key = sample_info$sample_key, subject_id = sample_info$subject_id,
                       timepoint = sample_info$timepoint, detection_failure_fraction = sample_info$detection_failure_fraction,
                       sample_qc_flag = sample_info$sample_qc_flag),
            file.path(output_root, "sample_qc.tsv"), sep = "\t", quote = FALSE, row.names = FALSE, na = "NA")
pdf(file.path(output_root, "methylation_qc.pdf"), width = 9, height = 6)
boxplot(as.data.frame(paired$beta[paired$probe_keep, , drop = FALSE]), outline = FALSE, las = 2,
        cex.axis = 0.5, main = "GSE54643 beta values from GEO signal matrix")
boxplot(sample_info$detection_failure_fraction ~ sample_info$timepoint, xlab = "Timepoint",
        ylab = "Failed probe fraction", main = "Detection p-value QC")
dev.off()

sha256_file <- function(path) {
  output <- system2("sha256sum", shQuote(path), stdout = TRUE, stderr = TRUE)
  if (!length(output) || (!is.null(attr(output, "status")) && attr(output, "status") != 0L)) stop("SHA-256 failed for ", path)
  strsplit(output[[1L]], "[[:space:]]+")[[1L]][[1L]]
}
input_paths <- c(matrix_path, soft_path, extraction_manifest_path, download_manifest_path, lock_path, overlay_lock_path)
input_manifest <- lapply(input_paths, function(path) list(path = normalizePath(path, mustWork = TRUE), bytes = file.info(path)$size, sha256 = sha256_file(path)))
script_snapshot <- file.path(output_root, basename(script_path))
if (!file.copy(script_path, script_snapshot, overwrite = TRUE)) stop("Could not preserve the analysis script snapshot.")
helper_snapshot <- file.path(output_root, "gse54643_methylation_helpers.R")
if (!file.copy(file.path(dirname(script_path), "gse54643_methylation_helpers.R"), helper_snapshot, overwrite = TRUE)) stop("Could not preserve the helper script snapshot.")
metadata <- list(
  accession = "GSE54643", analysis_version = arg_value("--analysis-version", "gse54643-processed-signal-paired-limma-v1"),
  run_utc = format(Sys.time(), tz = "UTC", usetz = TRUE), script_path = script_path,
  script_sha256 = sha256_file(script_path), helper_sha256 = sha256_file(helper_snapshot),
  environment_lock_path = lock_path, environment_lock_sha256 = sha256_file(lock_path),
  overlay_lock_path = overlay_lock_path, overlay_lock_sha256 = sha256_file(overlay_lock_path),
  input_manifest = input_manifest, sample_count = nrow(sample_info), independent_people = length(unique(sample_info$subject_id)),
  timepoints = list("baseline", "8w"), diet_arms = list("single uncontrolled cohort"),
  assay_annotation = "Illumina HumanMethylation450 BeadChip / hg19",
  normalization = "Noob normalization unavailable: GEO provides methylated/unmethylated signal and detection p-value matrix, not sample IDATs. Beta=M/(M+U+100); M=log2((M+1)/(U+1)); paired deltas tested with limma empirical Bayes.",
  probe_filter = "Detection p <= 0.01 in at least 95% of 20 samples and at least 8 complete within-person pairs per CpG.",
  cell_composition_adjusted = FALSE, dmr_analysis = "Not performed; CpG-level results only.",
  design_limit = "Uncontrolled pre-post pilot in 10 male smokers; time trends and intervention cannot be separated. Processed matrix has SN participant labels but no GSM crosswalk; no individual cross-omics merge was attempted.",
  result_files = list("methylation_effects.tsv.gz"),
  auxiliary_files = list("sample_metadata.tsv", "sample_qc.tsv", "methylation_qc.pdf", basename(script_snapshot), basename(helper_snapshot)),
  effect_scope = "Exploratory cohort-level paired methylation associations; not causal, individualized, or health-risk predictions.",
  software_versions = list(R = as.character(getRversion()), limma = as.character(packageVersion("limma")), minfi = as.character(packageVersion("minfi")), annotation = as.character(packageVersion("IlluminaHumanMethylation450kanno.ilmn12.hg19")))
)
write_json(metadata, file.path(output_root, "analysis_metadata.json"), pretty = TRUE, auto_unbox = TRUE)
cat(toJSON(list(status = "completed", accession = "GSE54643", samples = nrow(sample_info),
                independent_people = length(unique(sample_info$subject_id)), retained_cpgs = nrow(results),
                paired_design = TRUE, sample_qc_flags = as.list(table(sample_info$sample_qc_flag))),
           pretty = TRUE, auto_unbox = TRUE), "\n")
