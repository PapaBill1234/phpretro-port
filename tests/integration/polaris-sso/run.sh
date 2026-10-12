#!/usr/bin/env bash
set -euo pipefail
: "${POLARIS_SOURCE:?Set POLARIS_SOURCE to an explicit local Polaris git repository}"
JAVA_HOME="${JAVA_HOME:-/home/ubuntu/.local/opt/phpretro-tools/jdk-25.0.4.1+1}"
MAVEN_BIN="${MAVEN_BIN:-/home/ubuntu/.local/opt/phpretro-tools/apache-maven-3.10.0/bin/mvn}"
PIN=0101ce955069af1bcf1dd4299b03250679d9b1e1
PATCH="$(pwd)/integrations/polaris/patches/0001-strict-sso-consumer.patch"
[[ -x "$JAVA_HOME/bin/java" && -x "$MAVEN_BIN" && -f "$PATCH" ]] || { echo 'Java 25, Maven, or canonical patch missing' >&2; exit 2; }
git -C "$POLARIS_SOURCE" cat-file -e "$PIN^{commit}"
work="$(mktemp -d "$(pwd)/.builder/polaris-sso.XXXXXX")"
name="phpretro-sso-${RANDOM}-$$"
cleanup(){ docker rm -f "$name" >/dev/null 2>&1 || true; rm -rf "$work"; }
stop(){ trap - EXIT INT TERM; cleanup; exit 130; }
trap cleanup EXIT
trap stop INT TERM
git clone -q --local "$POLARIS_SOURCE" "$work/source"
git -C "$work/source" checkout -q "$PIN"
git -C "$work/source" apply --check "$PATCH"
git -C "$work/source" apply "$PATCH"
cp tests/integration/polaris-sso/StrictConsumerMariaDbProbe.java "$work/source/Emulator/src/test/java/com/eu/habbo/database/"
cp tests/integration/polaris-sso/DuplicateTicketMariaDbRegression.java "$work/source/Emulator/src/test/java/com/eu/habbo/database/"
export JAVA_HOME
timeout 300s "$MAVEN_BIN" -q -f "$work/source/Emulator/pom.xml" -DskipTests -DskipITs -Dcheckstyle.skip compile test-compile dependency:build-classpath -Dmdep.outputFile="$work/classpath"
cp="$work/source/Emulator/target/classes:$work/source/Emulator/target/test-classes:$(<"$work/classpath")"
timeout 120s "$JAVA_HOME/bin/javac" -cp "$cp" -d "$work/source/Emulator/target/test-classes" "$work/source/Emulator/src/test/java/com/eu/habbo/database/StrictConsumerMariaDbProbe.java" "$work/source/Emulator/src/test/java/com/eu/habbo/database/DuplicateTicketMariaDbRegression.java"
timeout 300s timeout 180s "$MAVEN_BIN" -q -f "$work/source/Emulator/pom.xml" -Dtest=SsoTicketConsumerBehaviorTest,StrictSsoStartupPreflightTest,GameClientManagerContractTest test
fixture_password="$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')"
docker run --rm -d --name "$name" -e "MARIADB_ROOT_PASSWORD=$fixture_password" -p 127.0.0.1::3306 "${MARIADB_IMAGE:-mariadb:10.11}" --skip-name-resolve >/dev/null
port="$(docker port "$name" 3306/tcp | awk -F: '{print $NF}')"
for i in $(seq 1 60); do docker exec "$name" mariadb-admin --protocol=tcp -uroot -p"$fixture_password" ping --silent >/dev/null 2>&1 && break; sleep 1; done
docker exec "$name" mariadb-admin --protocol=tcp -uroot -p"$fixture_password" ping --silent >/dev/null
docker exec "$name" mariadb --protocol=tcp -uroot -p"$fixture_password" -e "CREATE DATABASE strict_probe; CREATE DATABASE duplicate_probe;"
export G03_TEST_PASSWORD="$fixture_password"
export G03_TEST_DSN="jdbc:mariadb://127.0.0.1:${port}/strict_probe"
timeout 120s "$JAVA_HOME/bin/java" -cp "$cp" com.eu.habbo.database.StrictConsumerMariaDbProbe
export G03_TEST_DSN="jdbc:mariadb://127.0.0.1:${port}/duplicate_probe"
timeout 120s "$JAVA_HOME/bin/java" -cp "$cp" com.eu.habbo.database.DuplicateTicketMariaDbRegression
