script_argument <- grep("^--file=", commandArgs(trailingOnly = FALSE), value = TRUE)
if (length(script_argument)) {
  project_root <- normalizePath(file.path(dirname(sub("^--file=", "", script_argument[[1L]])), ".."))
  r_library <- Sys.getenv(
    "NUTRIGENOMICS_R_LIB",
    file.path(project_root, "..", "toolchain", "r-library")
  )
  if (dir.exists(r_library)) .libPaths(c(normalizePath(r_library), .libPaths()))
}

if (Sys.getenv("R_THREADS") != "1") {
  stop("Launch with R_THREADS=1 so RMA preprocessing uses a single pthread.")
}

suppressPackageStartupMessages(library(preprocessCore))
test_matrix <- matrix(seq_len(400), nrow = 100L, ncol = 4L)
normalized <- normalize.quantiles(test_matrix)
if (!identical(dim(normalized), dim(test_matrix)) || any(!is.finite(normalized))) {
  stop("preprocessCore quantile normalization smoke test failed.")
}

cat(
  "preprocessCore", as.character(packageVersion("preprocessCore")),
  "R_THREADS", Sys.getenv("R_THREADS"), "\n"
)
