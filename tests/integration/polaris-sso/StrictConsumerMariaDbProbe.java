package com.eu.habbo.database;
import com.eu.habbo.Emulator;
import com.eu.habbo.habbohotel.users.SsoTicketConsumer;
import com.zaxxer.hikari.*;
import java.sql.*;
import java.lang.reflect.*;
import java.util.*;
import java.util.concurrent.*;
public final class StrictConsumerMariaDbProbe {
 static class Pool extends HikariDataSource {
  volatile String fault="NONE";
  Pool(HikariConfig c){super(c);}
  @Override public Connection getConnection() throws SQLException {
   Connection real=super.getConnection();
   return (Connection)Proxy.newProxyInstance(Connection.class.getClassLoader(),new Class<?>[]{Connection.class},(p,m,a)->{
    if(m.getName().equals("commit")&&!fault.equals("NONE")){
     String kind=fault;fault="NONE";
     if(kind.equals("AFTER"))real.commit();
     throw new SQLException("Synthetic commit boundary fault");
    }
    try{return m.invoke(real,a);}catch(InvocationTargetException e){throw e.getCause();}
   });
  }
 }
 static Pool pool;static int checks;
 static void check(boolean ok,String name){if(!ok)throw new AssertionError(name);checks++;System.out.println("PASS "+name);}
 static void sql(String query)throws Exception{try(Connection c=pool.getConnection();Statement s=c.createStatement()){s.execute(query);}}
 static String ticket(int id)throws Exception{try(Connection c=pool.getConnection();PreparedStatement s=c.prepareStatement("SELECT auth_ticket FROM users WHERE id=?")){s.setInt(1,id);try(ResultSet r=s.executeQuery()){r.next();return r.getString(1);}}}
 static void row(int id,String value,String expiry)throws Exception{
  try(Connection c=pool.getConnection();PreparedStatement s=c.prepareStatement("INSERT INTO users(id,auth_ticket,auth_ticket_expires_at) VALUES(?,?,"+expiry+") ON DUPLICATE KEY UPDATE auth_ticket=VALUES(auth_ticket),auth_ticket_expires_at=VALUES(auth_ticket_expires_at)")){s.setInt(1,id);s.setString(2,value);s.executeUpdate();}
 }
 static void live(int id,String value)throws Exception{row(id,value,"DATE_ADD(CURRENT_TIMESTAMP,INTERVAL 10 MINUTE)");}
 public static void main(String[] ignored)throws Exception{
  HikariConfig c=new HikariConfig();c.setJdbcUrl(System.getenv("G03_TEST_DSN"));c.setUsername("root");c.setPassword(System.getenv("G03_TEST_PASSWORD"));c.setMaximumPoolSize(20);c.setMinimumIdle(0);c.setConnectionTimeout(10000);
  try(Pool opened=new Pool(c)){
   pool=opened;Field f=Emulator.class.getDeclaredField("database");f.setAccessible(true);f.set(null,new Database(pool));
   sql("CREATE TABLE users(id INT UNSIGNED PRIMARY KEY,auth_ticket VARCHAR(256) CHARACTER SET latin1 COLLATE latin1_swedish_ci,auth_ticket_expires_at TIMESTAMP NULL DEFAULT NULL,KEY auth_ticket (auth_ticket)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci");
   live(1,"CaseSensitive-Ticket");check(SsoTicketConsumer.consume("casesensitive-ticket").isEmpty(),"binary case mismatch rejected");check(ticket(1).equals("CaseSensitive-Ticket"),"case mismatch preserves current ticket");check(SsoTicketConsumer.consume("CaseSensitive-Ticket").orElse(0)==1,"exact live ticket returns correct identity");check(ticket(1).isEmpty(),"successful commit clears ticket");check(SsoTicketConsumer.consume("CaseSensitive-Ticket").isEmpty(),"replay rejected");
   row(2,"null-expiry","NULL");check(SsoTicketConsumer.consume("null-expiry").isEmpty(),"null expiry rejected");
   row(3,"expired","DATE_SUB(CURRENT_TIMESTAMP,INTERVAL 1 MINUTE)");check(SsoTicketConsumer.consume("expired").isEmpty(),"expired ticket rejected");
   live(4,"Whitespace-Ticket");for(String value:Arrays.asList(" Whitespace-Ticket","Whitespace-Ticket ","Whitespace-Ticket\t","é", "",null))check(SsoTicketConsumer.consume(value).isEmpty(),"invalid input rejected");check(ticket(4).equals("Whitespace-Ticket"),"invalid input preserves valid ticket");
   live(5,"x".repeat(128));check(SsoTicketConsumer.consume("x".repeat(128)).orElse(0)==5,"128-byte boundary accepted");live(6,"x".repeat(129));check(SsoTicketConsumer.consume("x".repeat(129)).isEmpty(),"129-byte input rejected");check(ticket(6).length()==129,"oversized rejection leaves DB unchanged");
   live(7,"new-ticket");check(SsoTicketConsumer.consume("old-ticket").isEmpty(),"stale generation rejected");check(ticket(7).equals("new-ticket"),"stale generation cannot clear newer ticket");
   live(8,"race-ticket");ExecutorService workers=Executors.newFixedThreadPool(16);CountDownLatch start=new CountDownLatch(1);List<Future<Boolean>> results=new ArrayList<>();for(int i=0;i<16;i++)results.add(workers.submit(()->{start.await();return SsoTicketConsumer.consume("race-ticket").orElse(0)==8;}));start.countDown();int successes=0;for(Future<Boolean> r:results)if(r.get(20,TimeUnit.SECONDS))successes++;workers.shutdownNow();check(successes==1,"16 concurrent consumers authenticate exactly once");check(ticket(8).isEmpty(),"race winner committed clearing");
   live(9,"commit-before");pool.fault="BEFORE";check(SsoTicketConsumer.consume("commit-before").isEmpty(),"pre-commit failure fails closed");check(ticket(9).equals("commit-before"),"pre-commit failure rolls back");check(SsoTicketConsumer.consume("commit-before").orElse(0)==9,"rolled-back ticket can later commit once");
   live(10,"commit-after");pool.fault="AFTER";check(SsoTicketConsumer.consume("commit-after").isEmpty(),"ambiguous post-commit failure fails closed");check(ticket(10).isEmpty(),"post-commit failure does not restore consumed ticket");check(SsoTicketConsumer.consume("commit-after").isEmpty(),"post-commit failure never enables replay");
   System.out.println("TOTAL_PASS="+checks);
  }
 }
}
