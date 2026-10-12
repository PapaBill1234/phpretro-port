package com.eu.habbo.database;
import com.eu.habbo.Emulator;
import com.eu.habbo.habbohotel.users.SsoTicketConsumer;
import com.zaxxer.hikari.*;
import java.sql.*;
import java.lang.reflect.*;
import java.util.*;
import java.util.concurrent.*;
public final class DuplicateTicketMariaDbRegression {
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
   live(11,"duplicate-live-ticket");live(12,"duplicate-live-ticket");
   String before;
   try(Connection x=pool.getConnection();Statement q=x.createStatement();ResultSet rs=q.executeQuery("SELECT GROUP_CONCAT(CONCAT(id,':',auth_ticket,':',auth_ticket_expires_at) ORDER BY id),@@tx_isolation FROM users")){
    rs.next();before=rs.getString(1);System.out.println("ISOLATION="+rs.getString(2));
   }
   for(int i=0;i<4;i++)check(SsoTicketConsumer.consume("duplicate-live-ticket").isEmpty(),"repeat ambiguity denies both identities");
   ExecutorService workers=Executors.newFixedThreadPool(16);CountDownLatch start=new CountDownLatch(1);List<Future<Boolean>> results=new ArrayList<>();
   for(int i=0;i<16;i++)results.add(workers.submit(()->{start.await();return SsoTicketConsumer.consume("duplicate-live-ticket").isPresent();}));
   start.countDown();int successes=0;for(Future<Boolean> result:results)if(result.get(20,TimeUnit.SECONDS))successes++;workers.shutdownNow();
   check(successes==0,"16 concurrent ambiguous consumers all deny");
   try(Connection x=pool.getConnection();Statement q=x.createStatement();ResultSet rs=q.executeQuery("SELECT GROUP_CONCAT(CONCAT(id,':',auth_ticket,':',auth_ticket_expires_at) ORDER BY id) FROM users")){
    rs.next();check(before.equals(rs.getString(1)),"duplicate IDs tickets expiries unchanged");
   }
   row(12,"duplicate-live-ticket","DATE_SUB(CURRENT_TIMESTAMP,INTERVAL 1 MINUTE)");
   check(SsoTicketConsumer.consume("duplicate-live-ticket").orElse(0)==11,"one live plus expired duplicate consumes only live");
   check(ticket(12).equals("duplicate-live-ticket"),"expired duplicate preserved");
   System.out.println("TOTAL_PASS="+checks);
  }
 }
}
