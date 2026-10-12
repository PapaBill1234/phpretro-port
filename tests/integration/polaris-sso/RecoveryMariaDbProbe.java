package com.eu.habbo.session;
import com.zaxxer.hikari.*;
import java.sql.*;
import java.time.*;
import java.security.*;
import java.lang.reflect.*;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.*;
public final class RecoveryMariaDbProbe {
 static int passes,failures;static Pool pool;
 static final Instant BASE=Instant.parse("2026-10-12T12:00:00.123Z");
 static class TestClock extends Clock { volatile Instant now=BASE; public ZoneId getZone(){return ZoneOffset.UTC;} public Clock withZone(ZoneId z){return this;} public Instant instant(){return now;} }
 static final TestClock clock=new TestClock();
 static class Pool extends HikariDataSource {
  final AtomicReference<String> fault=new AtomicReference<>("NONE");
  final Map<Connection,Connection> aliases=Collections.synchronizedMap(new IdentityHashMap<>());
  final AtomicInteger aborts=new AtomicInteger(),evictions=new AtomicInteger(),unsafeResets=new AtomicInteger(),activationUpdates=new AtomicInteger();
  volatile int failActivationAt;
  final AtomicBoolean pauseActivation=new AtomicBoolean();
  volatile CountDownLatch activationEntered,activationRelease;
  @Override public void evictConnection(Connection c){evictions.incrementAndGet();Connection real=aliases.remove(c);super.evictConnection(real==null?c:real);}
  Pool(HikariConfig c){super(c);}
  @Override public Connection getConnection()throws SQLException {
   Connection real=super.getConnection();boolean[] badRollback={false},badAbort={false},badReset={false};
   Connection proxy=(Connection)Proxy.newProxyInstance(Connection.class.getClassLoader(),new Class<?>[]{Connection.class},(p,m,a)->{
    if(m.getName().equals("commit")){
     String kind=fault.getAndSet("NONE");
     if(!kind.equals("NONE")){
      if(kind.equals("AFTER"))real.commit();
      if(kind.equals("RESET_FAIL")){real.commit();badReset[0]=true;return null;}
      if(kind.equals("ROLLBACK_FAIL")||kind.equals("ABORT_FAIL"))badRollback[0]=true;
      if(kind.equals("ABORT_FAIL"))badAbort[0]=true;
      throw new SQLException("fixture commit failure");
     }
    }
    if(m.getName().equals("rollback")&&badRollback[0])throw new SQLException("fixture rollback failure");
    if(m.getName().equals("abort")){aborts.incrementAndGet();if(badAbort[0])throw new SQLException("fixture abort failure");}
    if(m.getName().equals("setAutoCommit")&&Boolean.TRUE.equals(a[0])){
     if(badRollback[0])unsafeResets.incrementAndGet();
     if(badReset[0]){badReset[0]=false;throw new SQLException("fixture reset failure");}
    }
    if(m.getName().equals("close"))aliases.remove((Connection)p);
    if(m.getName().equals("prepareStatement")&&a[0] instanceof String sql&&sql.startsWith("UPDATE session_recovery_tickets SET recoverable_until")){
     if(pauseActivation.compareAndSet(true,false)){
      activationEntered.countDown();
      if(!activationRelease.await(10,TimeUnit.SECONDS))throw new SQLException("fixture activation barrier timeout");
     }
     PreparedStatement statement;
     try{statement=(PreparedStatement)m.invoke(real,a);}catch(InvocationTargetException e){throw e.getCause();}
     return Proxy.newProxyInstance(PreparedStatement.class.getClassLoader(),new Class<?>[]{PreparedStatement.class},(sp,sm,sa)->{
      if(sm.getName().equals("executeUpdate")&&activationUpdates.incrementAndGet()==failActivationAt)throw new SQLException("fixture second activation batch failure");
      try{return sm.invoke(statement,sa);}catch(InvocationTargetException e){throw e.getCause();}
     });
    }
    try{return m.invoke(real,a);}catch(InvocationTargetException e){throw e.getCause();}
   });
   aliases.put(proxy,real);return proxy;
  }
 }
 static void check(boolean yes,String label){if(yes){passes++;System.out.println("PASS "+label);}else{failures++;System.out.println("FAIL "+label);}}
 static SessionRecoveryService service(){return new SessionRecoveryService(new JdbcRecoveryTicketStore(pool),clock,new SecureRandom(),Duration.ofSeconds(120));}
 static String active(SessionRecoveryService s,int id){clock.now=BASE;String t=s.issue(id);s.checkpoint(List.of(id));return t;}
 static boolean field(int id,String column)throws Exception {try(Connection c=pool.getConnection();PreparedStatement q=c.prepareStatement("SELECT "+column+" FROM session_recovery_tickets WHERE user_id=?")){q.setInt(1,id);try(ResultSet r=q.executeQuery()){r.next();return r.getTimestamp(1)!=null;}}}
 @SuppressWarnings("unchecked") static AtomicReference<SessionRecoveryService> runtime()throws Exception{Field f=SessionRecoveryRuntime.class.getDeclaredField("SERVICE");f.setAccessible(true);return (AtomicReference<SessionRecoveryService>)f.get(null);}
 public static void main(String[] args)throws Exception{
  HikariConfig cfg=new HikariConfig();cfg.setJdbcUrl(System.getenv("G03_TEST_DSN"));cfg.setUsername("root");cfg.setPassword(System.getenv("G03_TEST_PASSWORD"));cfg.setMaximumPoolSize(20);cfg.setMinimumIdle(0);cfg.setConnectionTimeout(10000);cfg.setConnectionInitSql("SET time_zone='+00:00'");
  try(Pool opened=new Pool(cfg)){pool=opened;
   try(Connection c=pool.getConnection();Statement st=c.createStatement()){st.execute(java.nio.file.Files.readString(java.nio.file.Path.of(args[0])));}
   SessionRecoveryService s=service();String token=s.issue(1);
   check(s.consume(token).isEmpty(),"uncheckpointed token denies");check(!field(1,"recoverable_until"),"issuance has no recovery window");
   s.checkpoint(List.of(1,99));check(s.consume(token).orElse(0)==1,"checkpointed token authenticates correct identity");check(s.consume(token).isEmpty(),"replay denies");
   s=service();String old=active(s,2);String replacement=s.issue(2);
   check(s.consume(old).isEmpty(),"replacement rejects old generation");check(s.consume(replacement).isEmpty(),"replacement is not prematurely active");s.checkpoint(List.of(2));check(s.consume(replacement).orElse(0)==2,"replacement can be checkpointed once");
   SessionRecoveryService race=service();String racing=active(race,3);ExecutorService workers=Executors.newFixedThreadPool(16);CountDownLatch start=new CountDownLatch(1);
   try{List<Future<Boolean>> results=new ArrayList<>();for(int i=0;i<16;i++)results.add(workers.submit(()->{start.await();return race.consume(racing).orElse(0)==3;}));start.countDown();int wins=0;for(Future<Boolean> f:results)if(f.get(20,TimeUnit.SECONDS))wins++;check(wins==1,"16 recovery consumers authenticate exactly once");}finally{workers.shutdownNow();}
   check(field(3,"consumed_at"),"race consumption persists");
   s=service();token=active(s,4);clock.now=BASE.plusSeconds(120).minusMillis(1);check(s.consume(token).orElse(0)==4,"one millisecond before expiry accepts");
   s=service();token=active(s,5);clock.now=BASE.plusSeconds(120).plusMillis(1);check(s.consume(token).isEmpty(),"past expiry denies");
   s=service();token=active(s,6);clock.now=BASE.plusSeconds(120);check(s.consume(token).isEmpty(),"strict equality expiry denies");
   clock.now=BASE;SessionRecoveryService a=service(),b=service();a.issue(7);String foreign=b.issue(7);a.checkpoint(List.of(7));check(b.consume(foreign).isEmpty(),"stale service cannot activate replacement from another instance");
   AtomicReference<SessionRecoveryService> ref=runtime();SessionRecoveryService previous=ref.get();
   try{
    s=service();token=active(s,8);ref.set(s);pool.fault.set("BEFORE");check(SessionRecoveryRuntime.consume(token).isEmpty(),"precommit error fails closed");check(!field(8,"consumed_at"),"precommit error rolls back");check(s.consume(token).orElse(0)==8,"rolled back token can commit once");
    s=service();token=active(s,9);ref.set(s);pool.fault.set("AFTER");check(SessionRecoveryRuntime.consume(token).isEmpty(),"ambiguous postcommit error fails closed");check(field(9,"consumed_at"),"postcommit error does not restore token");check(s.consume(token).isEmpty(),"postcommit replay denies");
    s=service();token=active(s,10);ref.set(s);pool.fault.set("ROLLBACK_FAIL");check(SessionRecoveryRuntime.consume(token).isEmpty(),"failed rollback returns no identity");check(!field(10,"consumed_at"),"failed rollback must not implicitly commit consumption");
    clock.now=BASE;s=service();s.issue(11);ref.set(s);pool.fault.set("ROLLBACK_FAIL");SessionRecoveryRuntime.checkpoint(List.of(11));check(!field(11,"recoverable_until"),"failed rollback must not implicitly commit checkpoint activation");
    s=service();token=active(s,12);ref.set(s);int evictedBefore=pool.evictions.get();pool.fault.set("ABORT_FAIL");check(SessionRecoveryRuntime.consume(token).isEmpty(),"abort failure returns no identity");check(!field(12,"consumed_at"),"abort failure cannot persist consumption");check(pool.evictions.get()>evictedBefore,"abort failure still evicts physical pooled connection");
    s=service();token=active(s,13);ref.set(s);pool.fault.set("RESET_FAIL");check(SessionRecoveryRuntime.consume(token).isEmpty(),"reset failure after commit returns no identity");check(field(13,"consumed_at"),"reset failure preserves committed consumption");check(s.consume(token).isEmpty(),"reset failure cannot enable replay");
    check(pool.aborts.get()>=3,"rollback failures trigger physical abort attempts");check(pool.unsafeResets.get()==0,"unresolved transactions never reset autocommit");
    SessionRecoveryService concurrent=service();clock.now=BASE;concurrent.issue(14);pool.activationEntered=new CountDownLatch(1);pool.activationRelease=new CountDownLatch(1);pool.pauseActivation.set(true);
    ExecutorService writers=Executors.newFixedThreadPool(2);
    try{
     Future<?> checkpoint=writers.submit(()->concurrent.checkpoint(List.of(14)));if(!pool.activationEntered.await(10,TimeUnit.SECONDS))throw new AssertionError("activation did not enter");
     Future<String> newer=writers.submit(()->concurrent.issue(14));String newest=newer.get(10,TimeUnit.SECONDS);pool.activationRelease.countDown();checkpoint.get(10,TimeUnit.SECONDS);
     check(concurrent.consume(newest).isEmpty(),"old checkpoint cannot prematurely activate concurrent replacement");concurrent.checkpoint(List.of(14));check(concurrent.consume(newest).orElse(0)==14,"checkpoint cleanup preserves concurrently issued generation");
    }finally{pool.activationRelease.countDown();writers.shutdownNow();}
    SessionRecoveryService batches=service();clock.now=BASE;List<Integer> ids=new ArrayList<>();for(int id=5000;id<5501;id++){batches.issue(id);ids.add(id);}pool.activationUpdates.set(0);pool.failActivationAt=2;boolean batchFailed=false;
    try{batches.checkpoint(ids);}catch(SessionRecoveryException expected){batchFailed=true;}finally{pool.failActivationAt=0;}
    check(batchFailed,"second checkpoint batch fault is reported");
    try(Connection c=pool.getConnection();Statement q=c.createStatement();ResultSet rows=q.executeQuery("SELECT COUNT(*) FROM session_recovery_tickets WHERE user_id>=5000 AND recoverable_until IS NOT NULL")){rows.next();check(rows.getInt(1)==0,"501-user checkpoint rolls back first batch atomically");}
    batches.checkpoint(ids);
    try(Connection c=pool.getConnection();Statement q=c.createStatement();ResultSet rows=q.executeQuery("SELECT COUNT(*) FROM session_recovery_tickets WHERE user_id>=5000 AND recoverable_until IS NOT NULL")){rows.next();check(rows.getInt(1)==501,"failed checkpoint retains generations for safe retry");}
    ref.set(null);check(SessionRecoveryRuntime.consume("x".repeat(43)).isEmpty(),"disabled runtime denies");check(SessionRecoveryRuntime.issue(12).isEmpty(),"disabled runtime issues no credential");
   }finally{ref.set(previous);}
   System.out.println("TOTAL_PASS="+passes+" TOTAL_FAIL="+failures);
  }
  if(failures!=0)System.exit(42);
 }
}
