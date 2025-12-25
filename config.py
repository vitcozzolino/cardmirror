# TODO: Scale costs based on the table from BL
# https://docs.google.com/spreadsheets/d/1E2iSRhhNUjaZ3MblXx_rflpOIkMwK40OgX5X6yXE4zw/edit#gid=1033599937
cost_per_card = {"normal": 2, "holo": 2.2}

# URI of cards list
# Examples of input are:
# - "https://docs.google.com/spreadsheets/d/xxxxxxxxxxxxx/export?format=csv&#gid=0"
# - "res/examples.csv"
URI = "https://docs.google.com/spreadsheets/d/1b2YVUgpXOJn2tykYbHju2F7nmfODqS3Ap2rYvjn58Pw/export?format=csv&gid=0"

# Set to False to add to the output list all matches found
# for each card (useful when you want edit manually the output
# list to select a specific version/art)
EXIT_AT_FIRST_MATCH = True

# Loggin level
LOG_LEVEL = "DEBUG"
