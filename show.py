import sqlite3, sys
c = sqlite3.connect('/Users/user/Projects/fleet-dashboard/fleet.db')
rows = list(c.execute(
    "SELECT lane,model,http,tools,COALESCE(err,'') FROM health ORDER BY lane,model"))
print(len(rows), "rows")
for r in rows:
    print(f"{r[0]:<11}{r[1]:<48}http={str(r[2]):<6}tools={str(r[3]):<6}{r[4][:34]}")
