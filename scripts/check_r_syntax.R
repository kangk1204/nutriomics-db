args <- commandArgs(trailingOnly = TRUE)
if (!length(args)) stop("Provide one or more R source paths to parse.")

for (path in args) {
  parse(file = path)
  cat("parsed", normalizePath(path), "\n")
}
