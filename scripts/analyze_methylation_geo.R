script_argument <- grep("^--file=", commandArgs(trailingOnly = FALSE), value = TRUE)
if (!length(script_argument)) stop("Run as an Rscript file.")
source_root <- normalizePath(file.path(dirname(sub("^--file=", "", script_argument[[1L]])), ".."))
project_root <- normalizePath(file.path(source_root, ".."))
r_library <- Sys.getenv("NUTRIGENOMICS_R_LIB", file.path(project_root, "toolchain", "r-library"))
if (dir.exists(r_library)) .libPaths(c(normalizePath(r_library), .libPaths()))
suppressPackageStartupMessages({
  library(minfi)
  library(limma)
  library(jsonlite)
})

args <- commandArgs(trailingOnly = TRUE)
arg_value <- function(name, default) {
  index <- match(name, args)
  if (is.na(index) || index == length(args)) default else args[[index + 1L]]
}
accession <- toupper(arg_value("--accession", ""))
if (!accession %in% c("GSE107205", "GSE219217")) stop("Supported accessions: GSE107205, GSE219217.")
data_root <- normalizePath(arg_value("--data-dir", Sys.getenv("NUTRIGENOMICS_DATA_DIR", "data")), mustWork = TRUE)
output_root <- arg_value("--output-dir", file.path(data_root, "..", "outputs", paste0(tolower(accession), "-methylation-v1")))
sample_sheet_path <- arg_value("--sample-sheet", file.path(data_root, "..", "outputs", "geo-design-audit", accession, "sample_design.tsv"))
raw_root <- file.path(data_root, "processed", "geo", accession, "raw_files")
if (!file.exists(sample_sheet_path)) stop("Run scripts.audit_geo_design before methylation analysis.")
dir.create(output_root, recursive = TRUE, showWarnings = FALSE)

sample_info <- read.delim(sample_sheet_path, check.names = FALSE, stringsAsFactors = FALSE)
expected_samples <- if (accession == "GSE107205") 72L else 54L
expected_people <- if (accession == "GSE107205") 36L else 18L
if (nrow(sample_info) != expected_samples || anyDuplicated(sample_info$sample_accession)) stop("Unexpected or duplicate GEO samples.")
sample_info$subject_id <- as.character(sample_info$subject_candidate)
sample_info$timepoint <- as.character(sample_info$timepoint_candidate)
if (anyNA(sample_info$subject_id) || anyNA(sample_info$timepoint)) stop("A sample lacks an audited subject or timepoint candidate.")

if (accession == "GSE107205") {
  sample_info$diet_arm <- sub("#[^#]+$", "", sample_info$subject_id)
  sample_info$person_id <- sample_info$subject_id
  expected_timepoints <- c("baseline", "5y")
  arm_codes <- c("low-fat diet" = "LF", "meddiet+evoo" = "EVOO", "meddiet+nuts" = "NUTS")
  if (!setequal(unique(sample_info$diet_arm), names(arm_codes))) stop("Unexpected audited diet-arm labels.")
  if (!setequal(unique(sample_info$timepoint), expected_timepoints)) stop("Expected baseline and 5-year visits.")
  sample_info$condition_id <- paste0(unname(arm_codes[sample_info$diet_arm]), "_", sample_info$timepoint)
  contrast_specs <- list(
    list(name = "lowfat_5y_vs_baseline", base = "LF_baseline", post = "LF_5y", arm = "low-fat diet", followup = "5y"),
    list(name = "meddiet_evoo_5y_vs_baseline", base = "EVOO_baseline", post = "EVOO_5y", arm = "meddiet+evoo", followup = "5y"),
    list(name = "meddiet_nuts_5y_vs_baseline", base = "NUTS_baseline", post = "NUTS_5y", arm = "meddiet+nuts", followup = "5y")
  )
  if (arg_value("--bridge-contrasts", "false") == "true") {
    contrast_specs <- list(
      list(name = "evoo_vs_lowfat_5y_change", base = "EVOO_baseline", post = "EVOO_5y", arm = "meddiet+evoo", reference_arm = "low-fat diet", reference_base = "LF_baseline", reference_post = "LF_5y", followup = "5y"),
      list(name = "nuts_vs_lowfat_5y_change", base = "NUTS_baseline", post = "NUTS_5y", arm = "meddiet+nuts", reference_arm = "low-fat diet", reference_base = "LF_baseline", reference_post = "LF_5y", followup = "5y")
    )
  }
  annotation_package <- "IlluminaHumanMethylation450kanno.ilmn12.hg19"
  intervention_label <- "Five-year diet-arm pre-post contrasts; attrition and subgroup selection need publication-level interpretation"
} else {
  sample_info$diet_arm <- "single_cohort"
  sample_info$person_id <- sample_info$subject_id
  expected_timepoints <- c("baseline", "30d", "60d")
  if (!setequal(unique(sample_info$timepoint), expected_timepoints)) stop("Expected baseline, 30-day, and 60-day visits.")
  sample_info$condition_id <- sample_info$timepoint
  contrast_specs <- list(
    list(name = "30d_vs_baseline", base = "baseline", post = "30d", arm = "single_cohort", followup = "30d"),
    list(name = "60d_vs_baseline", base = "baseline", post = "60d", arm = "single_cohort", followup = "60d")
  )
  annotation_package <- "IlluminaHumanMethylationEPICanno.ilm10b4.hg19"
  intervention_label <- "Mediterranean diet plus fiber supplementation, uncontrolled pre-post cohort"
}
if (length(unique(sample_info$person_id)) != expected_people) stop("Unexpected independent-person count.")
if (anyDuplicated(paste(sample_info$subject_id, sample_info$timepoint, sep = "|"))) stop("Duplicate subject by visit records.")
for (spec in contrast_specs) {
  arm_rows <- sample_info$diet_arm == spec$arm
  counts <- table(factor(sample_info$timepoint[arm_rows], levels = expected_timepoints))
  if (any(counts != expected_people / if (accession == "GSE107205") 3 else 1)) stop(paste("Unbalanced visits:", spec$arm))
}

idat_files <- list.files(raw_root, pattern = "^GSM[0-9]+_[0-9]+_R[0-9]+C[0-9]+_(Gn|Grn|Green|Red)\\.idat\\.gz$", full.names = TRUE)
if (length(idat_files) != expected_samples * 2L) stop("Expected two gzipped IDAT channels per sample.")
idat_match <- regmatches(basename(idat_files), regexec("^(GSM[0-9]+)_([0-9]+)_(R[0-9]+C[0-9]+)_(Gn|Grn|Green|Red)\\.idat\\.gz$", basename(idat_files), ignore.case = TRUE))
if (any(lengths(idat_match) != 5L)) stop("Unable to parse all IDAT filenames.")
idat_meta <- do.call(rbind, lapply(idat_match, function(value) value[-1L]))
idat_channel <- ifelse(tolower(idat_meta[, 4L]) == "red", "Red", "Grn")
if (anyDuplicated(paste(idat_meta[, 1L], idat_channel, sep = "|"))) stop("Duplicate GEO IDAT channel.")

scratch <- tempfile(paste0(tolower(accession), "-idat-"))
dir.create(scratch, recursive = TRUE)
targets <- data.frame(Sample_Name = sample_info$sample_accession, Sentrix_ID = NA_character_, Sentrix_Position = NA_character_, Basename = NA_character_)
for (index in seq_len(nrow(sample_info))) {
  gsm <- sample_info$sample_accession[[index]]
  channel_index <- which(idat_meta[, 1L] == gsm)
  if (length(channel_index) != 2L || !setequal(idat_channel[channel_index], c("Grn", "Red"))) stop(paste("Missing IDAT pair:", gsm))
  if (length(unique(idat_meta[channel_index, 2L])) != 1L || length(unique(idat_meta[channel_index, 3L])) != 1L) stop(paste("IDAT channel barcode mismatch:", gsm))
  sentrix <- idat_meta[channel_index[[1L]], 2L]
  position <- idat_meta[channel_index[[1L]], 3L]
  base <- file.path(scratch, paste(gsm, sentrix, position, sep = "_"))
  targets$Sentrix_ID[[index]] <- sentrix
  targets$Sentrix_Position[[index]] <- position
  targets$Basename[[index]] <- base
  for (channel in c("Grn", "Red")) {
    source_index <- channel_index[idat_channel[channel_index] == channel]
    input <- gzfile(idat_files[[source_index]], open = "rb")
    output <- file(paste0(base, "_", channel, ".idat"), open = "wb")
    repeat {
      block <- readBin(input, what = "raw", n = 1024L * 1024L)
      if (!length(block)) break
      writeBin(block, output)
    }
    close(input)
    close(output)
  }
}
rg <- read.metharray.exp(targets = targets, extended = TRUE, force = TRUE, verbose = TRUE)
unlink(scratch, recursive = TRUE)
minfi_sample_ids <- sampleNames(rg)
target_basename_ids <- basename(targets$Basename)
if (anyDuplicated(minfi_sample_ids) || !setequal(minfi_sample_ids, target_basename_ids)) {
  stop(paste("minfi IDs do not match the constructed IDAT basenames:", paste(head(minfi_sample_ids, 5L), collapse = ", ")))
}
sample_order <- match(minfi_sample_ids, target_basename_ids)
sample_info <- sample_info[sample_order, , drop = FALSE]
sampleNames(rg) <- sample_info$sample_accession
detection_p <- detectionP(rg)
sample_info$detection_failure_fraction <- colMeans(detection_p > 0.01, na.rm = TRUE)
sample_info$sample_qc_flag <- ifelse(sample_info$detection_failure_fraction > 0.05, "review", "pass")
probe_keep <- rowMeans(detection_p <= 0.01, na.rm = TRUE) >= 0.95
if (sum(probe_keep) < 100000L) stop("Too few probes pass the 95% detection criterion.")

methyl_set <- mapToGenome(preprocessNoob(rg, dyeCorr = TRUE, verbose = TRUE))
beta_all <- getBeta(methyl_set)
m_values_all <- getM(methyl_set)
if (!identical(dim(beta_all), dim(m_values_all))) stop("Beta and M-value matrices have different dimensions.")
passed_detection_probe_ids <- rownames(detection_p)[
  rowMeans(detection_p <= 0.01, na.rm = TRUE) >= 0.95
]
probe_keep <- intersect(rownames(beta_all), passed_detection_probe_ids)
if (length(probe_keep) < 100000L) stop("Too few mapped probes pass the 95% detection criterion.")
beta <- beta_all[probe_keep, , drop = FALSE]
m_values <- m_values_all[probe_keep, , drop = FALSE]
if (!identical(dim(beta), dim(m_values)) || any(!is.finite(m_values))) stop("Invalid beta or M-value matrix.")
probe_annotation <- as.data.frame(getAnnotation(methyl_set), stringsAsFactors = FALSE)
annotation_index <- if ("Name" %in% names(probe_annotation)) match(rownames(beta), as.character(probe_annotation$Name)) else match(rownames(beta), rownames(probe_annotation))
if (sum(!is.na(annotation_index)) < 0.95 * nrow(beta)) stop("CpG annotation crosswalk is incomplete.")
probe_annotation <- probe_annotation[annotation_index, , drop = FALSE]
gene_column <- intersect(c("UCSC_RefGene_Name", "UCSC_RefGene_Name.1"), names(probe_annotation))
gene_label <- if (length(gene_column)) as.character(probe_annotation[[gene_column[[1L]]]]) else rep(NA_character_, nrow(beta))
chr_column <- intersect(c("chr", "CHR", "chromosome"), names(probe_annotation))
pos_column <- intersect(c("pos", "MAPINFO", "position"), names(probe_annotation))
chromosome <- if (length(chr_column)) as.character(probe_annotation[[chr_column[[1L]]]]) else rep(NA_character_, nrow(beta))
position <- if (length(pos_column)) as.character(probe_annotation[[pos_column[[1L]]]]) else rep(NA_character_, nrow(beta))

sample_info$condition_id <- factor(sample_info$condition_id)
sample_info$subject_id <- factor(sample_info$subject_id)
design <- model.matrix(~ 0 + condition_id, data = sample_info)
design_conditions <- sub("^condition_id", "", colnames(design))
design_name_map <- stats::setNames(make.names(design_conditions, unique = TRUE), design_conditions)
colnames(design) <- unname(design_name_map[design_conditions])
if (qr(design)$rank != ncol(design)) stop("Methylation design matrix is not full rank.")
contrast_expr <- vapply(contrast_specs, function(spec) {
  main <- paste(design_name_map[[spec$post]], "-", design_name_map[[spec$base]])
  if (is.null(spec$reference_arm)) main else paste0("(",main,") - (",design_name_map[[spec$reference_post]]," - ",design_name_map[[spec$reference_base]],")")
}, character(1))
names(contrast_expr) <- vapply(contrast_specs, function(spec) spec$name, character(1))
contrast_matrix <- makeContrasts(contrasts = contrast_expr, levels = design)
if (arg_value("--bridge-contrasts","false")=="true" && accession=="GSE107205") {
  people<-unique(as.character(sample_info$person_id))
  baseline_index<-match(paste(people,"baseline"),paste(sample_info$person_id,sample_info$timepoint))
  followup_index<-match(paste(people,"5y"),paste(sample_info$person_id,sample_info$timepoint))
  if(anyNA(baseline_index)||anyNA(followup_index))stop("Unmatched people in paired-change design")
  person_info<-sample_info[baseline_index,]
  person_info$arm_code<-factor(unname(arm_codes[person_info$diet_arm]),levels=c("LF","EVOO","NUTS"))
  design<-model.matrix(~0+arm_code,person_info);colnames(design)<-levels(person_info$arm_code)
  contrast_matrix<-makeContrasts(evoo_vs_lowfat_5y_change=EVOO-LF,nuts_vs_lowfat_5y_change=NUTS-LF,levels=design)
  delta_m<-m_values[,followup_index,drop=FALSE]-m_values[,baseline_index,drop=FALSE]
  paired_delta_beta<-beta[,followup_index,drop=FALSE]-beta[,baseline_index,drop=FALSE]
  fit<-eBayes(contrasts.fit(lmFit(delta_m,design),contrast_matrix),robust=TRUE)
  beta_fit<-eBayes(contrasts.fit(lmFit(paired_delta_beta,design),contrast_matrix),robust=TRUE)
  correlation<-list(consensus.correlation=NA_real_)
  beta_correlation<-list(consensus.correlation=NA_real_)
  saveRDS(list(delta_m=delta_m,delta_beta=paired_delta_beta,person_info=person_info,design=design,contrasts=contrast_matrix),
    file.path(output_root,"paired_change_inputs.rds"),compress=FALSE)
} else {
  correlation <- duplicateCorrelation(m_values, design, block = sample_info$subject_id)
  if (!is.finite(correlation$consensus.correlation)) stop("Repeated-measures correlation was not estimable.")
  fit <- eBayes(contrasts.fit(lmFit(m_values, design, block = sample_info$subject_id, correlation = correlation$consensus.correlation), contrast_matrix), robust = TRUE)
  beta_correlation <- duplicateCorrelation(beta, design, block = sample_info$subject_id)
  beta_fit <- eBayes(contrasts.fit(lmFit(beta, design, block = sample_info$subject_id, correlation = beta_correlation$consensus.correlation), contrast_matrix), robust = TRUE)
}

cpg_tables <- lapply(seq_along(contrast_specs), function(index) {
  spec <- contrast_specs[[index]]
  post_rows <- sample_info$diet_arm == spec$arm & sample_info$timepoint == spec$followup
  base_rows <- sample_info$diet_arm == spec$arm & sample_info$timepoint == "baseline"
  reference_rows <- rep(FALSE,nrow(sample_info))
  if (!is.null(spec$reference_arm)) reference_rows <- sample_info$diet_arm == spec$reference_arm
  delta_beta <- beta_fit$coefficients[,index]
  beta_standard_error <- beta_fit$stdev.unscaled[,index] * sqrt(beta_fit$s2.post)
  beta_critical <- qt(.975,df=beta_fit$df.total)
  context <- if (accession == "GSE107205") {
    if (is.null(spec$reference_arm)) paste(spec$arm, "five-year diet arm versus baseline") else paste(spec$arm,"versus",spec$reference_arm,"difference in five-year changes")
  } else paste("Mediterranean diet plus fiber,", spec$followup, "versus baseline; no control group")
  data.frame(
    feature_type = "cpg", feature_id = rownames(m_values),
    feature_label = ifelse(is.na(gene_label) | !nzchar(gene_label), rownames(m_values), gene_label),
    contrast = spec$name, tissue = "whole blood", timepoint = paste(spec$followup, "vs baseline"),
    intervention = context, dose = NA_character_, duration = spec$followup,
    effect_estimate = delta_beta, standard_error = beta_standard_error,
    confidence_low = delta_beta - beta_critical*beta_standard_error,
    confidence_high = delta_beta + beta_critical*beta_standard_error,p_value = fit$p.value[, index],
    q_value = p.adjust(fit$p.value[, index], method = "BH"), effect_unit = "delta beta", status = "measured",
    n_people = length(unique(sample_info$person_id[post_rows | base_rows | reference_rows])), n_samples = nrow(sample_info),
    contrast_assay_count = sum(post_rows | base_rows | reference_rows),
    m_value_estimate = fit$coefficients[, index], m_value_standard_error = fit$stdev.unscaled[, index] * sqrt(fit$s2.post),
    genome_build = "hg19", annotation_gene = gene_label, chromosome = chromosome, position = position,
    cell_composition_adjusted = FALSE, stringsAsFactors = FALSE
  )
})
cpg_results <- do.call(rbind, cpg_tables)
if (arg_value("--bridge-contrasts", "false") == "true") {
  cpg_results$q_value_contrast <- cpg_results$q_value
  cpg_results$q_value_study_family <- p.adjust(cpg_results$p_value,"BH")
  cpg_results$q_value <- cpg_results$q_value_study_family
}
write_gzip_tsv <- function(data, path) {
  connection <- gzfile(path, open = "wt")
  on.exit(close(connection))
  write.table(data, connection, sep = "\t", quote = FALSE, row.names = FALSE, na = "NA")
}
write_gzip_tsv(cpg_results, file.path(output_root, "cpg_effects.tsv.gz"))
write.table(sample_info, file.path(output_root, "sample_metadata.tsv"), sep = "\t", quote = FALSE, row.names = FALSE)
sample_qc <- data.frame(sample_accession = sample_info$sample_accession, subject_id = sample_info$subject_id, diet_arm = sample_info$diet_arm, timepoint = sample_info$timepoint, detection_failure_fraction = sample_info$detection_failure_fraction, sample_qc_flag = sample_info$sample_qc_flag)
write.table(sample_qc, file.path(output_root, "sample_qc.tsv"), sep = "\t", quote = FALSE, row.names = FALSE)
pdf(file.path(output_root, "methylation_qc.pdf"), width = 9, height = 6)
boxplot(as.data.frame(beta), outline = FALSE, las = 2, cex.axis = 0.4, main = paste(accession, "Noob beta values"))
boxplot(sample_qc$detection_failure_fraction ~ sample_qc$timepoint, xlab = "Timepoint", ylab = "Failed probe fraction", main = "Detection p-value QC")
dev.off()

`%||%` <- function(value, fallback) if (is.null(value)) fallback else value
sha256_file <- function(path) {
  output <- system2("sha256sum", shQuote(path), stdout = TRUE, stderr = TRUE)
  if (!length(output) || (attr(output, "status") %||% 0L) != 0L) stop(paste("SHA-256 failed for", path))
  strsplit(output[[1L]], "[[:space:]]+")[[1L]][[1L]]
}
script_path <- normalizePath(file.path("scripts", "analyze_methylation_geo.R"), mustWork = TRUE)
lock_path <- normalizePath(file.path(data_root, "..", "toolchain", "r-environment-linux-64.lock.txt"), mustWork = TRUE)
overlay_lock_path <- normalizePath(file.path(data_root, "..", "toolchain", "r-overlay-lock.txt"), mustWork = TRUE)
input_paths <- c(idat_files, sample_sheet_path, file.path(data_root, "raw", "geo", "download_manifest.json"), file.path(raw_root, "extraction_manifest.json"), lock_path, overlay_lock_path)
input_manifest <- lapply(input_paths, function(path) list(path = normalizePath(path, mustWork = TRUE), bytes = file.info(path)$size, sha256 = sha256_file(path)))
script_snapshot <- file.path(output_root, "analyze_methylation_geo.R")
if (!identical(normalizePath(script_path), normalizePath(script_snapshot, mustWork = FALSE))) {
  if (!file.copy(script_path, script_snapshot, overwrite = TRUE)) stop("Could not preserve the analysis script snapshot.")
}
metadata <- list(
  accession = accession, analysis_version = arg_value("--analysis-version", paste0(tolower(accession), "-noob-limma-cpg-", if (arg_value("--bridge-contrasts","false")=="true") "between-arm-v3" else if (accession == "GSE219217") "v2" else "v1")), run_utc = format(Sys.time(), tz = "UTC", usetz = TRUE),
  script_path = script_path, script_sha256 = sha256_file(script_path), environment_lock_path = lock_path, environment_lock_sha256 = sha256_file(lock_path),
  overlay_lock_path = overlay_lock_path, overlay_lock_sha256 = sha256_file(overlay_lock_path), input_manifest = input_manifest,
  sample_count = nrow(sample_info), independent_people = length(unique(sample_info$person_id)), timepoints = as.list(sort(unique(sample_info$timepoint))),
  diet_arms = as.list(sort(unique(sample_info$diet_arm))), repeated_measures_correlation = correlation$consensus.correlation,
  condition_design_names = as.list(design_name_map),
  assay_annotation = if (accession == "GSE107205") "450K / IlluminaHumanMethylation450kanno.ilmn12.hg19" else "EPIC / IlluminaHumanMethylationEPICanno.ilm10b4.hg19",
  normalization = "minfi preprocessNoob; M-values tested with limma; delta-beta reported as effect size",
  design_formula = if(arg_value("--bridge-contrasts","false")=="true") "One paired 5-year minus baseline change per person; arm means compared with limma (36 independent people)" else "Repeated-assay condition means with person duplicateCorrelation",
  effect_uncertainty = "SE and 95% CI from a separate beta-scale limma model with the identical contrast; M-value p-values retained",
  multiple_testing = if(arg_value("--bridge-contrasts","false")=="true") "BH across all CpG by two between-arm contrasts; within-contrast BH retained" else "BH within each contrast",
  probe_filter = "Mapped probes shared with detectionP and detected at p <= 0.01 in at least 95% of samples", cell_composition_adjusted = FALSE,
  dmr_analysis = "Not performed in this run; CpG-level effects only",
  design_limit = if (accession == "GSE219217") "Uncontrolled pre-post cohort; time trends cannot be separated from intervention." else intervention_label,
  result_files = list("cpg_effects.tsv.gz"), auxiliary_files = list("sample_metadata.tsv", "sample_qc.tsv", "methylation_qc.pdf", "analyze_methylation_geo.R"),
  effect_scope = "Cohort-level methylation association; not an individual CpG response or health-risk prediction",
  software_versions = list(R = as.character(getRversion()), minfi = as.character(packageVersion("minfi")), limma = as.character(packageVersion("limma")), annotation = as.character(packageVersion(annotation_package)))
)
write_json(metadata, file.path(output_root, "analysis_metadata.json"), pretty = TRUE, auto_unbox = TRUE)
cat(toJSON(list(status = "completed", accession = accession, samples = nrow(sample_info), independent_people = length(unique(sample_info$person_id)), retained_cpgs = length(probe_keep), contrasts = length(contrast_specs), cpg_effect_rows = nrow(cpg_results), repeated_correlation = correlation$consensus.correlation, sample_qc_flags = as.list(table(sample_qc$sample_qc_flag))), pretty = TRUE, auto_unbox = TRUE), "\n")
